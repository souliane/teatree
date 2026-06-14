"""``t3 <overlay> workspace reclaim-disk`` — the sanctioned safe-only disk reclaim.

The command frees disk via the three zero-data-loss Docker prunes and STOPS. It
must never reach for worktree teardown, ``clean-all``, or any active-stack
removal — those stay separate, explicitly-targeted actions. These tests pin the
boundary at the CLI seam: the command routes through
:func:`teatree.docker.reclaim.reclaim_disk` and touches no teardown path.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

import teatree.core.management.commands.workspace as workspace_mod
from teatree.docker.reclaim import PruneOutcome, ReclaimReport, ReclaimStep


def _stub_report(*, dry_run: bool = False) -> ReclaimReport:
    steps = (
        ReclaimStep(
            argv=["docker", "builder", "prune", "-af"],
            label="build cache",
            outcome=PruneOutcome(reclaimed="1.0GB", bytes_reclaimed=10**9),
        ),
        ReclaimStep(
            argv=["docker", "image", "prune", "-f"],
            label="dangling images",
            outcome=PruneOutcome(reclaimed="0B", bytes_reclaimed=0),
        ),
        ReclaimStep(
            argv=["docker", "volume", "prune", "-f"],
            label="unreferenced volumes",
            outcome=PruneOutcome(reclaimed="512MB", bytes_reclaimed=512 * 10**6),
        ),
    )
    return ReclaimReport(steps=steps if not dry_run else (), planned=steps, dry_run=dry_run)


class ReclaimDiskCommandTests(TestCase):
    def test_routes_through_the_safe_engine(self) -> None:
        with patch.object(workspace_mod, "reclaim_disk", return_value=_stub_report()) as engine:
            call_command("workspace", "reclaim-disk")
        engine.assert_called_once()
        assert engine.call_args.kwargs.get("dry_run") is False

    def test_dry_run_flag_forwarded(self) -> None:
        with patch.object(workspace_mod, "reclaim_disk", return_value=_stub_report(dry_run=True)) as engine:
            call_command("workspace", "reclaim-disk", dry_run=True)
        assert engine.call_args.kwargs.get("dry_run") is True

    def test_never_calls_worktree_teardown_or_clean_all(self) -> None:
        with (
            patch.object(workspace_mod, "reclaim_disk", return_value=_stub_report()),
            patch.object(workspace_mod, "WorktreeTeardownRunner") as teardown_runner,
            patch.object(workspace_mod, "reap_one_worktree") as reap_worktree,
            patch.object(workspace_mod, "reap_orphan_worktree_docker") as reap_orphan,
        ):
            call_command("workspace", "reclaim-disk")
        teardown_runner.assert_not_called()
        reap_worktree.assert_not_called()
        reap_orphan.assert_not_called()

    def test_output_reports_each_step_and_the_total(self) -> None:
        stdout = StringIO()
        with patch.object(workspace_mod, "reclaim_disk", return_value=_stub_report()):
            call_command("workspace", "reclaim-disk", stdout=stdout)
        printed = stdout.getvalue()
        assert "build cache" in printed
        assert "dangling images" in printed
        assert "unreferenced volumes" in printed
        assert "Total" in printed
