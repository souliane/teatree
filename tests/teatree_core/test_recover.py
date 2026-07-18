"""``t3 recover`` — outage recovery report + requeue actions (#1764).

The orphan classifier and reconcile pass probe git/the host CLI, so those seams
are mocked via a context manager; the task/ticket mapping and the requeue
mutation run against real DB rows. The boot sweeps are stubbed to a zero count
so the assertions cover pure reads.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.cli import recover as cli_recover
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.worktree.recover import RecoverReport, gather_recover_report, requeue_failed_tasks
from teatree.core.worktree.recovery_sweeps import BootSweepCounts


@contextmanager
def _mocked_probes(*, orphans: list[BranchReport] | None = None) -> Iterator[None]:
    """Stub the git/network/temp-scan seams so gather() runs against DB rows only."""
    with (
        patch("teatree.core.worktree.recover.run_boot_sweeps", return_value=BootSweepCounts()),
        patch("teatree.core.worktree.recover.reconcile_all", return_value={}),
        patch("teatree.core.worktree.recover.find_orphans_in_workspace", return_value=orphans or []),
    ):
        yield


def _failed_outage_task(*, state: str = Ticket.State.STARTED, url: str = "https://x/issues/1") -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state, issue_url=url)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    task = Task.objects.create(ticket=ticket, session=session, phase="coding")
    task.claim(claimed_by="loop")
    TaskAttempt.objects.create(task=task, execution_target=task.execution_target, error="outage_death: socket")
    task.fail()
    return task


class TestGatherRecoverReport(TestCase):
    def test_classifies_orphans_into_three_groups(self) -> None:
        orphans = [
            BranchReport(repo="/r", branch="b-unpushed", status=BranchStatus.UNPUSHED_ORPHAN, ahead_count=2),
            BranchReport(repo="/r", branch="b-pushed", status=BranchStatus.PUSHED_ORPHAN, ahead_count=1),
            BranchReport(
                repo="/r",
                branch="b-pr",
                status=BranchStatus.OPEN_PR,
                ahead_count=3,
                open_pr_url="https://x/pull/9",
            ),
        ]
        with _mocked_probes(orphans=orphans):
            report = gather_recover_report()

        assert [o.branch for o in report.data_loss_risk] == ["b-unpushed"]
        assert [o.branch for o in report.committed_unpushed] == ["b-pushed"]
        assert [o.branch for o in report.open_pr_pending] == ["b-pr"]
        assert report.open_pr_pending[0].open_pr_url == "https://x/pull/9"

    def test_surfaces_outage_failed_task_as_requeue_candidate(self) -> None:
        task = _failed_outage_task()
        with _mocked_probes():
            report = gather_recover_report()

        assert len(report.requeue_candidates) == 1
        assert report.requeue_candidates[0].task_pk == task.pk
        assert report.requeue_candidates[0].is_outage is True
        assert report.requeue_candidates[0].ticket_url == "https://x/issues/1"

    def test_terminal_ticket_failed_task_is_not_a_candidate(self) -> None:
        _failed_outage_task(state=Ticket.State.MERGED)
        with _mocked_probes():
            report = gather_recover_report()

        assert report.requeue_candidates == []

    def test_unknown_overlay_failed_task_is_not_a_candidate(self) -> None:
        ticket = Ticket.objects.create(
            role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, overlay="ghost-overlay", issue_url="https://x/i/8"
        )
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.claim(claimed_by="loop")
        TaskAttempt.objects.create(task=task, execution_target=task.execution_target, error="boom")
        task.fail()

        with _mocked_probes():
            report = gather_recover_report()

        assert report.requeue_candidates == []

    def test_empty_report_has_no_findings(self) -> None:
        with _mocked_probes():
            report = gather_recover_report()

        assert report.has_findings is False
        assert "(no stranded work found)" in report.to_terse(dry_run=True)


class TestToTerse(TestCase):
    def test_renders_every_group_with_clickable_refs(self) -> None:
        from teatree.core.worktree.recover import OrphanItem, RequeueCandidate  # noqa: PLC0415

        report = RecoverReport(boot_sweeps=BootSweepCounts(replayed_transitions=1, reclaimed_claims=2, reaped_claims=3))
        report.data_loss_risk.append(OrphanItem(repo="/r", branch="b1", ahead_count=4, ticket_url="https://x/i/1"))
        report.open_pr_pending.append(OrphanItem(repo="/r", branch="b2", ahead_count=1, open_pr_url="https://x/pull/2"))
        report.requeue_candidates.append(
            RequeueCandidate(
                task_pk=7,
                ticket_url="https://x/i/9",
                phase="coding",
                error="outage_death: socket",
                is_outage=True,
            ),
        )
        report.drift_ticket_pks = [11]

        out = report.to_terse(dry_run=True)

        assert "DRY RUN" in out
        assert "replayed=1 reclaimed=2 reaped=3" in out
        assert "https://x/i/1" in out
        assert "https://x/pull/2" in out  # the PR url wins over the ticket url
        assert "TODO-7" in out
        assert "task#7" not in out
        assert "[outage]" in out
        assert "Reconcile drift on tickets: teatree#11" in out
        assert "tickets: #11" not in out

    def test_orphan_with_no_url_renders_placeholder(self) -> None:
        from teatree.core.worktree.recover import OrphanItem  # noqa: PLC0415

        report = RecoverReport()
        report.committed_unpushed.append(OrphanItem(repo="/r", branch="b", ahead_count=1))

        assert "(no url)" in report.to_terse(dry_run=False)
        assert "applied" in report.to_terse(dry_run=False)


class TestBranchToTicketUrl(TestCase):
    def test_maps_resolvable_clones_and_skips_unresolvable(self) -> None:
        from teatree.core.models import Worktree  # noqa: PLC0415
        from teatree.core.worktree.recover import _branch_to_ticket_url  # noqa: PLC0415

        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, issue_url="https://x/i/55")
        Worktree.objects.create(
            overlay="t", ticket=ticket, repo_path="r1", branch="feat-a", extra={"clone_path": "/c1"}
        )
        Worktree.objects.create(overlay="t", ticket=ticket, repo_path="r2", branch="feat-b", extra={})

        def _resolve(_ws: Path, wt: Worktree) -> Path | None:
            return Path("/c1") if wt.branch == "feat-a" else None

        with (
            patch("teatree.core.worktree.recover.clone_root"),
            patch("teatree.core.worktree.recover.resolve_clone_path", side_effect=_resolve),
        ):
            mapping = _branch_to_ticket_url()

        assert mapping == {("/c1", "feat-a"): "https://x/i/55"}


class TestRequeueFailedTasks(TestCase):
    def test_reopens_only_still_failed_tasks(self) -> None:
        task = _failed_outage_task(url="https://x/i/2")
        with _mocked_probes():
            report = gather_recover_report()

        reopened = requeue_failed_tasks(report)

        task.refresh_from_db()
        assert reopened == [task.pk]
        assert task.status == Task.Status.PENDING

    def test_skips_task_completed_by_concurrent_actor(self) -> None:
        task = _failed_outage_task(url="https://x/i/3")
        with _mocked_probes():
            report = gather_recover_report()
        task.reopen()
        task.claim(claimed_by="other")
        task.complete()

        reopened = requeue_failed_tasks(report)

        task.refresh_from_db()
        assert reopened == []
        assert task.status == Task.Status.COMPLETED


class TestRecoverCommand(TestCase):
    def test_dry_run_mutates_nothing(self) -> None:
        task = _failed_outage_task(url="https://x/i/4")
        with _mocked_probes():
            out = StringIO()
            call_command("recover", stdout=out)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "DRY RUN" in out.getvalue()

    def test_requeue_flag_reopens_outage_task(self) -> None:
        task = _failed_outage_task(url="https://x/i/5")
        with _mocked_probes():
            call_command("recover", "--requeue", stdout=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_json_output_shape(self) -> None:
        orphans = [BranchReport(repo="/r", branch="b", status=BranchStatus.UNPUSHED_ORPHAN, ahead_count=1)]
        with _mocked_probes(orphans=orphans):
            out = StringIO()
            call_command("recover", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["data_loss_risk"][0]["branch"] == "b"
        assert "boot_sweeps" in payload
        assert payload["reopened_task_pks"] == []


class TestRecoverCliForwarding(TestCase):
    def test_dry_run_forwards_no_flags(self) -> None:
        from types import SimpleNamespace  # noqa: PLC0415

        from teatree.cli import recover as cli_recover  # noqa: PLC0415

        active = SimpleNamespace(project_path=Path("/proj"), name="acme")
        with (
            patch("teatree.config.discover_active_overlay", return_value=active),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(requeue=False, overlay="")

        managepy.assert_called_once_with(Path("/proj"), "recover", overlay_name="acme")

    def test_requeue_forwards_the_flag(self) -> None:
        from types import SimpleNamespace  # noqa: PLC0415

        from teatree.cli import recover as cli_recover  # noqa: PLC0415

        active = SimpleNamespace(project_path=Path("/proj"), name="acme")
        with (
            patch("teatree.config.discover_active_overlay", return_value=active),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(requeue=True, overlay="")

        managepy.assert_called_once_with(Path("/proj"), "recover", "--requeue", overlay_name="acme")

    def test_json_flag_forwards_the_flag(self) -> None:
        """Regression: `t3 recover --json` must forward `--json` (parity with manage.py recover).

        The old ``ctx.args`` passthrough forwarded any flag; the explicit-option
        rewrite must keep declaring and forwarding ``--json`` or the documented
        shortcut breaks while the management command still supports it.
        """
        active = SimpleNamespace(project_path=Path("/proj"), name="acme")
        with (
            patch("teatree.config.discover_active_overlay", return_value=active),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(requeue=False, json_output=True, overlay="")

        managepy.assert_called_once_with(Path("/proj"), "recover", "--json", overlay_name="acme")

    def test_overlay_flag_overrides_active_overlay(self) -> None:
        from teatree.cli import recover as cli_recover  # noqa: PLC0415

        with (
            patch("teatree.config.discover_active_overlay", return_value=None),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(requeue=True, overlay="other")

        managepy.assert_called_once_with(None, "recover", "--requeue", overlay_name="other")

    def test_requeue_flag_parses_at_cli_not_read_as_subcommand(self) -> None:
        """Regression: `t3 recover --requeue` must PARSE, not fail 'No such command'.

        The prior raw ``ctx.args`` passthrough let Typer's group parser treat a
        leading ``--requeue`` as a subcommand name; declaring it as an explicit
        option fixes that.
        """
        from typer.testing import CliRunner  # noqa: PLC0415

        from teatree.cli.recover import recover_app  # noqa: PLC0415

        with (
            patch("teatree.config.discover_active_overlay", return_value=None),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            result = CliRunner().invoke(recover_app, ["--requeue"])

        assert result.exit_code == 0, result.output
        assert "No such command" not in result.output
        managepy.assert_called_once()
        assert "--requeue" in managepy.call_args.args
