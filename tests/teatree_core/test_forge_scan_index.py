"""One live forge and origin snapshot per repository during a branch sweep."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from teatree.core.forge_pr_probe import PrProbeOutcome, probe_gitlab_open_pr
from teatree.core.forge_scan_index import active_branch_scan_index, branch_scan_scope
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus, find_orphans_in_workspace
from teatree.core.worktree.reconcile import reconcile_work_state_all
from teatree.core.worktree.worktree_done import reap_done_worktrees_detailed

_REPO = "/tmp/teatree-index-test"


def test_open_pr_probe_reads_forge_again_inside_branch_scan_scope() -> None:
    responses = ["", "https://gitlab.example/mr/1"]
    with (
        patch("teatree.core.forge_pr_probe._gitlab_open_mr_url", side_effect=responses) as read,
        branch_scan_scope(),
    ):
        assert probe_gitlab_open_pr(_REPO, "feature").outcome is PrProbeOutcome.NONE
        assert probe_gitlab_open_pr(_REPO, "feature").url == "https://gitlab.example/mr/1"
    assert read.call_count == 2


def test_open_pr_probe_does_not_report_absence_for_blank_branch() -> None:
    with patch("teatree.core.forge_pr_probe._gitlab_open_mr_url", return_value="") as read, branch_scan_scope():
        assert probe_gitlab_open_pr(_REPO, "").outcome is PrProbeOutcome.UNKNOWN
    read.assert_not_called()


def test_fifty_origin_probes_share_one_complete_ref_list() -> None:
    result = MagicMock(returncode=0, stdout="a" * 40 + "\trefs/heads/feature-2\n")
    with (
        patch("teatree.core.forge_scan_index.run_with_status", return_value=result) as read,
        branch_scan_scope() as index,
    ):
        outcomes = [index.origin_has_branch(_REPO, f"feature-{number}", timeout=30) for number in range(50)]
    assert outcomes == [number == 2 for number in range(50)]
    read.assert_called_once()


def test_failed_origin_read_is_unknown_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    result = MagicMock(returncode=128, stdout="", stderr="unavailable")
    with (
        patch("teatree.core.forge_scan_index.run_with_status", return_value=result) as read,
        branch_scan_scope() as index,
    ):
        outcomes = [index.origin_has_branch(_REPO, f"feature-{number}", timeout=30) for number in range(50)]
    assert outcomes == [None] * 50
    read.assert_called_once()
    assert sum(_REPO in record.message for record in caplog.records) == 1


def test_orphan_workers_share_scan_scope() -> None:
    seen: list[object] = []

    def classify(repo: str, branch: str) -> BranchReport:
        seen.append(active_branch_scan_index())
        return BranchReport(repo=repo, branch=branch, status=BranchStatus.SYNCED, ahead_count=0)

    rows = [SimpleNamespace(branch=f"feature-{number}") for number in range(50)]
    with (
        patch("teatree.core.gates.orphan_guard.clone_root", return_value=Path("/tmp")),
        patch("teatree.core.gates.orphan_guard.resolve_clone_path", return_value=Path("/tmp")),
        patch("teatree.core.gates.orphan_guard.classify_branch", side_effect=classify),
    ):
        assert find_orphans_in_workspace(rows=rows) == []
    assert len(seen) == 50
    assert seen[0] is not None
    assert all(index is seen[0] for index in seen)


def test_reaper_shares_scan_scope_across_rows() -> None:
    seen: list[object] = []

    def reap(*_args: object, **_kwargs: object) -> object:
        seen.append(active_branch_scan_index())
        return object()

    with (
        patch("teatree.core.worktree.worktree_done.reset_forge_probe_cache"),
        patch("teatree.core.worktree.worktree_done.Worktree.objects.select_related", return_value=[object(), object()]),
        patch("teatree.core.worktree.worktree_done._reap_or_report", side_effect=reap),
    ):
        assert len(reap_done_worktrees_detailed(Path("/tmp"), dry_run=True)) == 2
    assert seen[0] is not None
    assert seen[0] is seen[1]


def test_reconcile_shares_scan_scope_across_tickets() -> None:
    seen: list[object] = []

    def collect(*_args: object, **_kwargs: object) -> None:
        seen.append(active_branch_scan_index())

    with (
        patch("teatree.core.worktree.reconcile.WorkStateScope.whole_board"),
        patch("teatree.core.worktree.reconcile.Worktree.objects.all", return_value=[]),
        patch(
            "teatree.core.worktree.reconcile.Ticket.objects.all",
            return_value=[SimpleNamespace(pk=1), SimpleNamespace(pk=2)],
        ),
        patch("teatree.core.worktree.reconcile._collect_work_state_drift", side_effect=collect),
    ):
        assert reconcile_work_state_all() == {}
    assert seen[0] is not None
    assert seen[0] is seen[1]
