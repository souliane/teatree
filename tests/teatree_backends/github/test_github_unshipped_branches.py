"""GitHub branch tip, merge and malformed-read decisions."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.backends.github import pr_reads
from teatree.backends.github.client import GitHubCodeHost
from teatree.utils.run import CommandFailedError


def test_merged_head_is_shipped_and_extended_head_is_unshipped() -> None:
    refs = [{"ref": "refs/heads/5346-work", "object": {"sha": "new"}}]
    merged = [{"merged_at": "2026-01-01", "head": {"sha": "new"}}]
    with (
        patch.object(pr_reads, "_gh_api_get", side_effect=[{"default_branch": "main"}, {"ahead_by": 1}]),
        patch.object(pr_reads, "_strict_list_pages", side_effect=[refs, merged]),
    ):
        assert GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-") == []
    extended = [{"merged_at": "2026-01-01", "head": {"sha": "old"}}]
    with (
        patch.object(pr_reads, "_gh_api_get", side_effect=[{"default_branch": "main"}, {"ahead_by": 1}]),
        patch.object(pr_reads, "_strict_list_pages", side_effect=[refs, extended]),
    ):
        names = GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-")
        assert names == ["5346-work"]


@pytest.mark.parametrize("closed_item", [{}, {"merged_at": "2026-01-01", "head": {}}, {"merged_at": None}])
def test_malformed_closed_pull_request_is_unverified(closed_item: dict[str, object]) -> None:
    refs = [{"ref": "refs/heads/5346-work", "object": {"sha": "new"}}]
    with (
        patch.object(pr_reads, "_gh_api_get", side_effect=[{"default_branch": "main"}, {"ahead_by": 1}]),
        patch.object(pr_reads, "_strict_list_pages", side_effect=[refs, [closed_item]]),
        pytest.raises(TypeError, match="invalid closed pull request"),
    ):
        GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-")


def test_non_list_page_raises() -> None:
    with (
        patch.object(pr_reads, "_run_gh", return_value=SimpleNamespace(stdout='[{"message":"error"}]')),
        pytest.raises(TypeError, match="non-list"),
    ):
        pr_reads._strict_list_pages("repos/x/y/pulls", token="x")


def test_http_500_raises_instead_of_reporting_no_branches() -> None:
    failure = CommandFailedError(["gh", "api"], 1, "", "HTTP 500")
    with (
        patch.object(pr_reads, "_gh_api_get", return_value={"default_branch": "main"}),
        patch.object(pr_reads, "_run_gh", side_effect=failure),
        pytest.raises(CommandFailedError),
    ):
        GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-")


def test_http_404_raises_instead_of_reporting_no_branches() -> None:
    failure = CommandFailedError(["gh", "api"], 1, "", "HTTP 404")
    with (
        patch.object(pr_reads, "_gh_api_get", return_value={"default_branch": "main"}),
        patch.object(pr_reads, "_run_gh", side_effect=failure),
        pytest.raises(CommandFailedError),
    ):
        GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-")


def test_matching_refs_empty_page_is_verified_empty() -> None:
    with (
        patch.object(pr_reads, "_gh_api_get", return_value={"default_branch": "main"}),
        patch.object(pr_reads, "_run_gh", return_value=SimpleNamespace(stdout="[[]]")),
    ):
        assert GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-") == []


def test_branch_with_no_commits_ahead_is_not_unshipped() -> None:
    refs = [{"ref": "refs/heads/5346-work", "object": {"sha": "same"}}]
    with (
        patch.object(pr_reads, "_gh_api_get", side_effect=[{"default_branch": "main"}, {"ahead_by": 0}]),
        patch.object(pr_reads, "_strict_list_pages", return_value=refs) as pages,
    ):
        assert GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-") == []
        pages.assert_called_once()


@pytest.mark.parametrize("metadata", [{}, {"default_branch": ""}, None])
def test_unreadable_default_branch_raises(metadata: object) -> None:
    with patch.object(pr_reads, "_gh_api_get", return_value=metadata), pytest.raises(TypeError, match="default branch"):
        GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-")


@pytest.mark.parametrize(
    "ref", [{"ref": "refs/tags/5346-work", "object": {"sha": "abc"}}, {"ref": "refs/heads/5346-work"}]
)
def test_unreadable_branch_ref_raises(ref: dict[str, object]) -> None:
    with (
        patch.object(pr_reads, "_gh_api_get", return_value={"default_branch": "main"}),
        patch.object(pr_reads, "_strict_list_pages", return_value=[ref]),
        pytest.raises(TypeError, match="invalid branch ref"),
    ):
        GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-")


def test_unrelated_ref_does_not_trigger_comparison() -> None:
    with (
        patch.object(pr_reads, "_gh_api_get", return_value={"default_branch": "main"}) as read,
        patch.object(
            pr_reads, "_strict_list_pages", return_value=[{"ref": "refs/heads/other", "object": {"sha": "abc"}}]
        ),
    ):
        assert GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-") == []
        read.assert_called_once()


def test_unreadable_comparison_raises() -> None:
    with (
        patch.object(pr_reads, "_gh_api_get", side_effect=[{"default_branch": "main"}, {}]),
        patch.object(
            pr_reads, "_strict_list_pages", return_value=[{"ref": "refs/heads/5346-work", "object": {"sha": "abc"}}]
        ),
        pytest.raises(TypeError, match="invalid branch comparison"),
    ):
        GitHubCodeHost(token="x").list_unshipped_branches(repo="souliane/teatree", prefix="5346-")
