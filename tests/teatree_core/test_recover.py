"""``t3 recover`` — outage recovery report + requeue actions (#1764).

The orphan classifier and reconcile pass probe git/the host CLI, so those seams
are mocked via a context manager; the task/ticket mapping and the requeue
mutation run against real DB rows. The boot sweeps are stubbed to a zero count
so the assertions cover pure reads.
"""

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.cli import recover as cli_recover
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.management.commands.recover import _DEFAULT_SINCE, RecoverPayload
from teatree.core.modelkit.durations import parse_duration
from teatree.core.modelkit.task_parking import HALT_STAMP, LIVE_SUCCESSOR_STAMP
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.worktree.recover import (
    DEFAULT_REQUEUE_WINDOW,
    RecoverReport,
    RecoverReportDict,
    RequeueThresholdError,
    gather_recover_report,
    requeue_failed_tasks,
)
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
    TaskAttempt.objects.create(task=task, error="outage_death: socket")
    task.fail(reason="test: deliberate failure")
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
        TaskAttempt.objects.create(task=task, error="boom")
        task.fail(reason="test: deliberate failure")

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


class TestDryRunHeaderTellsTheTruthAboutTheSweeps(TestCase):
    """The boot sweeps run on the default path and WRITE — the header may not deny it."""

    def test_a_sweep_that_recovered_rows_is_not_reported_as_nothing_changed(self) -> None:
        report = RecoverReport(boot_sweeps=BootSweepCounts(reclaimed_claims=2, reclaimed_leases=1))

        out = report.to_terse(dry_run=True)

        assert "nothing changed" not in out
        assert "3 row(s)" in out

    def test_a_sweep_that_touched_nothing_still_says_nothing_changed(self) -> None:
        report = RecoverReport(boot_sweeps=BootSweepCounts())

        assert "(nothing changed)" in report.to_terse(dry_run=True)


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
            err = StringIO()
            call_command("recover", stderr=err)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "DRY RUN" in err.getvalue()

    def test_requeue_flag_reopens_outage_task(self) -> None:
        task = _failed_outage_task(url="https://x/i/5")
        with _mocked_probes():
            call_command("recover", "--requeue", stdout=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_returns_exactly_the_declared_payload_shape(self) -> None:
        with _mocked_probes():
            payload = call_command("recover", stdout=StringIO(), stderr=StringIO())
        assert set(payload) == set(RecoverPayload.__annotations__) | set(RecoverReportDict.__annotations__)

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


def _failed_task(
    ticket: Ticket,
    *,
    phase: str = "coding",
    error: str = "boom",
    failed_ago: dt.timedelta | None = None,
) -> Task:
    """A FAILED task on *ticket*, its attempt backdated by *failed_ago* when given."""
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(ticket=ticket, session=session, phase=phase)
    task.claim(claimed_by="loop")
    attempt = TaskAttempt.objects.create(task=task, error=error)
    if failed_ago is not None:
        stamp = timezone.now() - failed_ago
        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=stamp, ended_at=stamp)
    task.fail(reason="test: deliberate failure")
    return task


def _started_ticket(url: str) -> Ticket:
    return Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, issue_url=url)


class TestRequeueWindow(TestCase):
    """#4710: a recovery run recovers the incident's casualties, not the whole deployment's history."""

    def test_a_failure_older_than_the_window_is_not_a_candidate(self) -> None:
        _failed_task(_started_ticket("https://x/i/100"), failed_ago=dt.timedelta(days=2))

        with _mocked_probes():
            report = gather_recover_report()

        assert report.requeue_candidates == []
        assert report.requeue_excluded.older_than_window == 1

    def test_a_wider_window_admits_the_same_failure(self) -> None:
        task = _failed_task(_started_ticket("https://x/i/101"), failed_ago=dt.timedelta(days=2))

        with _mocked_probes():
            report = gather_recover_report(since=dt.timedelta(days=3))

        assert [c.task_pk for c in report.requeue_candidates] == [task.pk]
        assert report.requeue_excluded.older_than_window == 0

    def test_an_undated_failure_is_excluded_rather_than_assumed_recent(self) -> None:
        ticket = _started_ticket("https://x/i/102")
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.claim(claimed_by="loop")
        task.fail(reason="test: deliberate failure")
        Task.objects.filter(pk=task.pk).update(created_at=None)

        with _mocked_probes():
            report = gather_recover_report()

        assert report.requeue_candidates == []
        assert report.requeue_excluded.older_than_window == 1

    def test_an_unbounded_gather_admits_every_age(self) -> None:
        task = _failed_task(_started_ticket("https://x/i/103"), failed_ago=dt.timedelta(days=900))

        with _mocked_probes():
            report = gather_recover_report(since=None)

        assert [c.task_pk for c in report.requeue_candidates] == [task.pk]


class TestRequeueDedupe(TestCase):
    def test_only_the_newest_attempt_per_ticket_phase_is_reopened(self) -> None:
        ticket = _started_ticket("https://x/i/110")
        _failed_task(ticket)
        _failed_task(ticket)
        newest = _failed_task(ticket)

        with _mocked_probes():
            report = gather_recover_report()

        assert [c.task_pk for c in report.requeue_candidates] == [newest.pk]
        assert report.requeue_excluded.duplicate_phase == 2

    def test_distinct_phases_on_one_ticket_each_keep_a_candidate(self) -> None:
        ticket = _started_ticket("https://x/i/111")
        coding = _failed_task(ticket, phase="coding")
        testing = _failed_task(ticket, phase="testing")

        with _mocked_probes():
            report = gather_recover_report()

        assert sorted(c.task_pk for c in report.requeue_candidates) == sorted([coding.pk, testing.pk])
        assert report.requeue_excluded.duplicate_phase == 0

    def test_a_phase_a_live_task_already_holds_is_skipped(self) -> None:
        ticket = _started_ticket("https://x/i/112")
        _failed_task(ticket, phase="coding")
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        Task.objects.create(ticket=ticket, session=session, phase="coding")

        with _mocked_probes():
            report = gather_recover_report()

        assert report.requeue_candidates == []
        assert report.requeue_excluded.live_successor == 1

    def test_a_parked_task_is_never_reopened(self) -> None:
        ticket = _started_ticket("https://x/i/113")
        halted = _failed_task(ticket, phase="coding")
        Task.objects.filter(pk=halted.pk).update(execution_reason=f"why {HALT_STAMP}")
        superseded = _failed_task(ticket, phase="testing")
        Task.objects.filter(pk=superseded.pk).update(execution_reason=f"why {LIVE_SUCCESSOR_STAMP}")

        with _mocked_probes():
            report = gather_recover_report()

        assert report.requeue_candidates == []


class TestRequeuePreview(TestCase):
    def test_the_preview_states_the_count_and_the_age_span(self) -> None:
        _failed_task(_started_ticket("https://x/i/120"), failed_ago=dt.timedelta(hours=3))
        _failed_task(_started_ticket("https://x/i/121"), failed_ago=dt.timedelta(hours=1))

        with _mocked_probes():
            report = gather_recover_report()

        preview = report.requeue_preview()

        assert "2 task(s)" in preview
        assert "2 ticket(s)" in preview
        assert "3h ago" in preview
        assert "1h ago" in preview

    def test_an_empty_candidate_set_says_so(self) -> None:
        with _mocked_probes():
            report = gather_recover_report()

        assert "0 task(s)" in report.requeue_preview()

    def test_the_terse_report_names_the_window_and_what_it_excluded(self) -> None:
        ticket = _started_ticket("https://x/i/122")
        _failed_task(ticket, failed_ago=dt.timedelta(days=5))
        _failed_task(ticket, failed_ago=dt.timedelta(hours=2))

        with _mocked_probes():
            report = gather_recover_report()

        out = report.to_terse(dry_run=True)

        assert "within 1d" in out
        assert "1 older than the window" in out
        assert "2h ago" in out


class TestRequeueThreshold(TestCase):
    def test_reopening_past_the_ceiling_is_refused_and_mutates_nothing(self) -> None:
        tasks = [_failed_task(_started_ticket(f"https://x/i/13{n}")) for n in range(3)]
        with _mocked_probes():
            report = gather_recover_report()

        with pytest.raises(RequeueThresholdError) as exc:
            requeue_failed_tasks(report, max_reopen=2)

        assert exc.value.count == 3
        assert exc.value.max_reopen == 2
        for task in tasks:
            task.refresh_from_db()
            assert task.status == Task.Status.FAILED

    def test_naming_the_count_lifts_the_ceiling(self) -> None:
        tasks = [_failed_task(_started_ticket(f"https://x/i/14{n}")) for n in range(3)]
        with _mocked_probes():
            report = gather_recover_report()

        reopened = requeue_failed_tasks(report, max_reopen=3)

        assert sorted(reopened) == sorted(t.pk for t in tasks)

    def test_a_count_equal_to_the_ceiling_is_allowed(self) -> None:
        tasks = [_failed_task(_started_ticket(f"https://x/i/15{n}")) for n in range(2)]
        with _mocked_probes():
            report = gather_recover_report()

        assert sorted(requeue_failed_tasks(report, max_reopen=2)) == sorted(t.pk for t in tasks)


class TestRecoverCommandBounds(TestCase):
    def test_requeue_prints_the_preview_before_reopening(self) -> None:
        _failed_task(_started_ticket("https://x/i/160"))
        err = StringIO()

        with _mocked_probes():
            call_command("recover", "--requeue", stdout=StringIO(), stderr=err)

        out = err.getvalue()
        assert out.index("Will reopen") < out.index("Reopened")

    def test_since_scopes_the_recovery(self) -> None:
        old = _failed_task(_started_ticket("https://x/i/161"), failed_ago=dt.timedelta(days=2))
        fresh = _failed_task(_started_ticket("https://x/i/162"))

        with _mocked_probes():
            payload = cast("RecoverPayload", call_command("recover", "--requeue", stdout=StringIO(), stderr=StringIO()))

        assert payload["reopened_task_pks"] == [fresh.pk]
        old.refresh_from_db()
        assert old.status == Task.Status.FAILED

    def test_over_the_max_refuses_non_zero_and_reopens_nothing(self) -> None:
        tasks = [_failed_task(_started_ticket(f"https://x/i/17{n}")) for n in range(3)]
        err = StringIO()

        with _mocked_probes(), pytest.raises(SystemExit) as exc:
            call_command("recover", "--requeue", "--max", "2", stdout=StringIO(), stderr=err)

        assert exc.value.code == 1
        assert "--max 3" in err.getvalue()
        for task in tasks:
            task.refresh_from_db()
            assert task.status == Task.Status.FAILED

    def test_a_raised_max_admits_the_same_batch(self) -> None:
        tasks = [_failed_task(_started_ticket(f"https://x/i/18{n}")) for n in range(3)]

        with _mocked_probes():
            payload = cast(
                "RecoverPayload",
                call_command("recover", "--requeue", "--max", "3", stdout=StringIO(), stderr=StringIO()),
            )

        assert sorted(payload["reopened_task_pks"]) == sorted(t.pk for t in tasks)

    def test_an_unparseable_since_is_refused_before_anything_is_reopened(self) -> None:
        task = _failed_task(_started_ticket("https://x/i/190"))
        err = StringIO()

        with _mocked_probes(), pytest.raises(SystemExit) as exc:
            call_command("recover", "--requeue", "--since", "1w", stdout=StringIO(), stderr=err)

        assert exc.value.code == 1
        assert "--since" in err.getvalue()
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_json_carries_the_window_and_the_exclusions(self) -> None:
        _failed_task(_started_ticket("https://x/i/191"), failed_ago=dt.timedelta(days=9))
        out = StringIO()

        with _mocked_probes():
            call_command("recover", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["requeue_window_seconds"] == int(dt.timedelta(hours=24).total_seconds())
        assert payload["requeue_excluded"]["older_than_window"] == 1
        assert payload["requeue_refused"] is False


class TestRecoverCliForwardsTheBounds(TestCase):
    def _managepy_args(self, **kwargs: object) -> tuple[object, ...]:
        active = SimpleNamespace(project_path=Path("/proj"), name="acme")
        with (
            patch("teatree.config.discover_active_overlay", return_value=active),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(**kwargs)
        return managepy.call_args.args

    def test_defaults_forward_nothing_extra(self) -> None:
        assert self._managepy_args(requeue=True) == (Path("/proj"), "recover", "--requeue")

    def test_since_and_max_are_forwarded(self) -> None:
        args = self._managepy_args(requeue=True, since="3d", max_reopen=5)

        assert args == (Path("/proj"), "recover", "--requeue", "--since", "3d", "--max", "5")


class TestTheDefaultWindowHasOneSourceOfTruth(TestCase):
    """The ``--since`` default is RENDERED from the core window, so the two cannot drift."""

    def test_the_flag_default_round_trips_to_the_core_window(self) -> None:
        assert parse_duration(_DEFAULT_SINCE, flag="--since") == DEFAULT_REQUEUE_WINDOW


class TestZeroIsNotTheSameAsUnset(TestCase):
    """``--max 0`` (refuse every reopen) must survive the forwarder, not read as 'unset'."""

    def test_max_zero_is_forwarded(self) -> None:
        active = SimpleNamespace(project_path=Path("/proj"), name="acme")
        with (
            patch("teatree.config.discover_active_overlay", return_value=active),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(requeue=True, max_reopen=0)

        assert managepy.call_args.args == (Path("/proj"), "recover", "--requeue", "--max", "0")

    def test_max_zero_refuses_any_reopen_at_the_command(self) -> None:
        task = _failed_task(_started_ticket("https://x/i/200"))
        err = StringIO()

        with _mocked_probes(), pytest.raises(SystemExit) as exc:
            call_command("recover", "--requeue", "--max", "0", stdout=StringIO(), stderr=err)

        assert exc.value.code == 1
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
