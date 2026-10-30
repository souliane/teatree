"""One live forge and origin snapshot per repository during a branch sweep."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from teatree.core.forge_pr_probe import PrProbe, PrProbeOutcome, probe_gitlab_open_pr
from teatree.core.gates.forge_scan_index import active_branch_scan_index, branch_scan_scope
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus, classify_branch, find_orphans_in_workspace

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
        patch("teatree.core.gates.forge_scan_index.run_with_status", return_value=result) as read,
        branch_scan_scope() as index,
    ):
        outcomes = [index.origin_has_branch(_REPO, f"feature-{number}", timeout=30) for number in range(50)]
    assert outcomes == [number == 2 for number in range(50)]
    read.assert_called_once()


def test_failed_origin_read_is_unknown_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    result = MagicMock(returncode=128, stdout="", stderr="unavailable")
    with (
        patch("teatree.core.gates.forge_scan_index.run_with_status", return_value=result) as read,
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
        branch_scan_scope() as index,
    ):
        assert find_orphans_in_workspace(rows=rows) == []
    assert len(seen) == 50
    assert seen[0] is index
    assert all(item is index for item in seen)
    assert active_branch_scan_index() is None


@contextmanager
def _classifying_unmerged_branches(probe: PrProbe) -> Generator[None]:
    ahead = SimpleNamespace(genuinely_ahead=["commit"], branch_missing=False)
    with (
        patch("teatree.core.gates.orphan_guard._origin_default_branch_target", return_value="origin/main"),
        patch("teatree.core.gates.orphan_guard.prefilter_branch_commits_by_subject", return_value=ahead),
        patch("teatree.core.gates.orphan_guard._local_content_verdict", return_value=None),
        patch("teatree.core.gates.orphan_guard._branch_tree_matches_squash", return_value=False),
        patch("teatree.core.gates.orphan_guard.find_open_pr_for_branch", return_value=probe),
        patch("teatree.core.gates.orphan_guard.branch_content_landed_on_base", return_value=False),
    ):
        yield


def test_unreadable_open_pr_state_inside_scope_warns_naming_the_repository(caplog: pytest.LogCaptureFixture) -> None:
    with _classifying_unmerged_branches(PrProbe.unknown()), branch_scan_scope():
        report = classify_branch(_REPO, "feature")
    assert report.status is BranchStatus.PR_UNKNOWN
    assert any(_REPO in record.message and record.levelname == "WARNING" for record in caplog.records)


def test_classifying_many_branches_in_a_scan_reads_origin_once_and_maps_each_branch() -> None:
    listing = MagicMock(returncode=0, stdout="a" * 40 + "\trefs/heads/pushed\n")
    with (
        _classifying_unmerged_branches(PrProbe.none()),
        patch("teatree.core.gates.forge_scan_index.run_with_status", return_value=listing) as shared_read,
        patch("teatree.core.gates.orphan_guard.run_with_status") as per_branch_read,
        branch_scan_scope(),
    ):
        statuses = [classify_branch(_REPO, branch).status for branch in ("pushed", "local-only", "also-local")]
    assert statuses == [BranchStatus.PUSHED_ORPHAN, BranchStatus.UNPUSHED_ORPHAN, BranchStatus.UNPUSHED_ORPHAN]
    shared_read.assert_called_once()
    per_branch_read.assert_not_called()


def test_an_unreadable_origin_listing_makes_every_branch_in_the_scan_remote_unknown() -> None:
    failed = MagicMock(returncode=128, stdout="", stderr="unavailable")
    with (
        _classifying_unmerged_branches(PrProbe.none()),
        patch("teatree.core.gates.forge_scan_index.run_with_status", return_value=failed),
        patch("teatree.core.gates.orphan_guard.run_with_status") as per_branch_read,
        branch_scan_scope(),
    ):
        statuses = {classify_branch(_REPO, branch).status for branch in ("one", "two")}
    assert statuses == {BranchStatus.REMOTE_UNKNOWN}
    per_branch_read.assert_not_called()
