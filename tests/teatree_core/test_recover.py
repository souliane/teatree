"""``t3 recover`` — outage recovery report + requeue/snapshot actions (#1764).

The orphan classifier and reconcile pass probe git/the host CLI, so those seams
are mocked via a context manager; the task/ticket mapping and the requeue
mutation run against real DB rows. The boot sweeps are stubbed to a zero count
so the assertions cover pure reads.
"""

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.recover import RecoverReport, _collect_stranded_snapshots, gather_recover_report, requeue_failed_tasks
from teatree.core.recovery_sweeps import BootSweepCounts


@contextmanager
def _mocked_probes(*, orphans: list[BranchReport] | None = None) -> Iterator[None]:
    """Stub the git/network/temp-scan seams so gather() runs against DB rows only."""
    with (
        patch("teatree.core.recover.run_boot_sweeps", return_value=BootSweepCounts()),
        patch("teatree.core.recover.reconcile_all", return_value={}),
        patch("teatree.core.recover._collect_stranded_snapshots"),
        patch("teatree.core.recover.find_orphans_in_workspace", return_value=orphans or []),
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
        from teatree.core.recover import OrphanItem, RequeueCandidate, StrandedSnapshot  # noqa: PLC0415

        report = RecoverReport(boot_sweeps=BootSweepCounts(replayed_transitions=1, reclaimed_claims=2, reaped_claims=3))
        report.data_loss_risk.append(OrphanItem(repo="/r", branch="b1", ahead_count=4, ticket_url="https://x/i/1"))
        report.open_pr_pending.append(OrphanItem(repo="/r", branch="b2", ahead_count=1, open_pr_url="https://x/pull/2"))
        report.stranded_snapshots.append(StrandedSnapshot(path=Path("/tmp/t3-recover-x")))
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
        assert "/tmp/t3-recover-x" in out
        assert "TODO-7" in out
        assert "task#7" not in out
        assert "[outage]" in out
        assert "Reconcile drift on tickets: teatree#11" in out
        assert "tickets: #11" not in out

    def test_orphan_with_no_url_renders_placeholder(self) -> None:
        from teatree.core.recover import OrphanItem  # noqa: PLC0415

        report = RecoverReport()
        report.committed_unpushed.append(OrphanItem(repo="/r", branch="b", ahead_count=1))

        assert "(no url)" in report.to_terse(dry_run=False)
        assert "applied" in report.to_terse(dry_run=False)


class TestStrandedSnapshots(TestCase):
    def test_collects_only_t3_recover_dirs_from_tempdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "t3-recover-1764-x").mkdir()
            (root / "unrelated-dir").mkdir()
            (root / "t3-recover-file-not-dir").write_text("x", encoding="utf-8")
            report = RecoverReport()
            with patch("teatree.core.recover.tempfile.gettempdir", return_value=str(root)):
                _collect_stranded_snapshots(report)

        assert {s.path.name for s in report.stranded_snapshots} == {"t3-recover-1764-x"}

    def test_missing_tempdir_is_a_noop(self) -> None:
        report = RecoverReport()
        with patch("teatree.core.recover.tempfile.gettempdir", return_value="/nonexistent/tmp/path"):
            _collect_stranded_snapshots(report)

        assert report.stranded_snapshots == []


class TestBranchToTicketUrl(TestCase):
    def test_maps_resolvable_clones_and_skips_unresolvable(self) -> None:
        from teatree.core.models import Worktree  # noqa: PLC0415
        from teatree.core.recover import _branch_to_ticket_url  # noqa: PLC0415

        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, issue_url="https://x/i/55")
        Worktree.objects.create(
            overlay="t", ticket=ticket, repo_path="r1", branch="feat-a", extra={"clone_path": "/c1"}
        )
        Worktree.objects.create(overlay="t", ticket=ticket, repo_path="r2", branch="feat-b", extra={})

        def _resolve(_ws: Path, wt: Worktree) -> Path | None:
            return Path("/c1") if wt.branch == "feat-a" else None

        with (
            patch("teatree.core.recover.clone_root"),
            patch("teatree.core.recover.resolve_clone_path", side_effect=_resolve),
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


class TestForceCaptureSnapshots(TestCase):
    def test_captures_only_worktrees_with_a_clone_and_a_path(self) -> None:
        from teatree.core.models import Worktree  # noqa: PLC0415
        from teatree.core.recover import force_capture_snapshots  # noqa: PLC0415

        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, issue_url="https://x/i/77")
        with_path = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch="feat-77",
            extra={"worktree_path": "/some/wt", "clone_path": "/some/clone"},
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="r2",
            branch="feat-77b",
            extra={"clone_path": "/c2"},  # has a clone but no worktree_path -> skipped
        )

        clean = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="r3",
            branch="feat-clean",
            extra={"worktree_path": "/clean/wt", "clone_path": "/c3"},
        )

        def _resolve(_ws: Path, wt: Worktree) -> Path | None:
            return {with_path.pk: Path("/some/clone"), clean.pk: Path("/c3")}.get(wt.pk, Path("/c2"))

        def _capture(_clone: Path, wt_path: str, *, branch: str, label: str) -> Path | None:
            return None if wt_path == "/clean/wt" else Path("/tmp/t3-recover-77")

        with (
            patch("teatree.core.recover.clone_root"),
            patch("teatree.core.recover.resolve_clone_path", side_effect=_resolve),
            patch("teatree.core.recover.capture_worktree_snapshot", side_effect=_capture) as cap,
        ):
            captured = force_capture_snapshots()

        # dirty -> captured; clean -> capture returned None (not appended); no-path -> skipped before capture.
        assert captured == [Path("/tmp/t3-recover-77")]
        assert cap.call_count == 2


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

    def test_snapshot_flag_captures_and_reports(self) -> None:
        with (
            _mocked_probes(),
            patch(
                "teatree.core.management.commands.recover.force_capture_snapshots",
                return_value=[Path("/tmp/t3-recover-z")],
            ),
        ):
            out = StringIO()
            call_command("recover", "--snapshot", stdout=out)

        body = out.getvalue()
        assert "Captured 1 snapshot(s)." in body
        assert "/tmp/t3-recover-z" in body
        assert "applied" in body  # not a dry run

    def test_json_output_shape(self) -> None:
        orphans = [BranchReport(repo="/r", branch="b", status=BranchStatus.UNPUSHED_ORPHAN, ahead_count=1)]
        with _mocked_probes(orphans=orphans):
            out = StringIO()
            call_command("recover", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["data_loss_risk"][0]["branch"] == "b"
        assert "boot_sweeps" in payload
        assert payload["reopened_task_pks"] == []
        assert payload["captured_snapshots"] == []


class TestSplitOverlayFlag(TestCase):
    def test_splits_space_and_equals_forms_and_keeps_rest(self) -> None:
        from teatree.cli.recover import _split_overlay_flag  # noqa: PLC0415

        assert _split_overlay_flag(["--overlay", "acme", "--json"]) == ("acme", ["--json"])
        assert _split_overlay_flag(["--overlay=acme", "--requeue"]) == ("acme", ["--requeue"])
        assert _split_overlay_flag(["--snapshot"]) == ("", ["--snapshot"])


class TestRecoverCliForwarding(TestCase):
    def test_forwards_to_managepy_with_resolved_overlay(self) -> None:
        from types import SimpleNamespace  # noqa: PLC0415

        from teatree.cli import recover as cli_recover  # noqa: PLC0415

        active = SimpleNamespace(project_path=Path("/proj"), name="acme")
        ctx = SimpleNamespace(args=["--json"])
        with (
            patch("teatree.config.discover_active_overlay", return_value=active),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(ctx)

        managepy.assert_called_once_with(Path("/proj"), "recover", "--json", overlay_name="acme")

    def test_overlay_flag_overrides_active_overlay(self) -> None:
        from types import SimpleNamespace  # noqa: PLC0415

        from teatree.cli import recover as cli_recover  # noqa: PLC0415

        ctx = SimpleNamespace(args=["--overlay", "other", "--requeue"])
        with (
            patch("teatree.config.discover_active_overlay", return_value=None),
            patch("teatree.cli.recover.managepy") as managepy,
        ):
            cli_recover.recover(ctx)

        managepy.assert_called_once_with(None, "recover", "--requeue", overlay_name="other")
