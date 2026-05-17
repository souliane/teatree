"""GitHub-sync, dual-sync and reviewer-PR tests (souliane/teatree#443 split of test_sync.py).

Covers _merge_results, dual GitHub+GitLab sync, reviewer MR/PR caching and
the GitHub sync backend.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import TestCase

from teatree.backends.gitlab_sync import GitLabSyncBackend
from teatree.core.models import Ticket, Worktree
from teatree.core.sync import _merge_results, sync_followup
from teatree.types import PENDING_REVIEWS_CACHE_KEY, SyncResult
from tests.teatree_core.sync._overlays import (
    _MR_WITH_ISSUE,
    _MR_WITHOUT_ISSUE,
    SyncOverlay,
    _make_mock_client,
    _patch_overlay,
)


class TestMergeResults:
    def test_sums_counts_and_concatenates_errors(self) -> None:
        a = SyncResult(prs_found=3, tickets_created=1, errors=["err-a"])
        b = SyncResult(prs_found=5, tickets_updated=2, errors=["err-b"])
        merged = _merge_results(a, b)
        assert merged.prs_found == 8
        assert merged.tickets_created == 1
        assert merged.tickets_updated == 2
        assert merged.errors == ["err-a", "err-b"]

    def test_merges_empty_results(self) -> None:
        merged = _merge_results(SyncResult(), SyncResult())
        assert merged.prs_found == 0
        assert merged.errors == []


_GITHUB_ITEM_URL = "https://github.com/souliane/teatree/issues/10"


class TestDualSync(TestCase):
    """When both GitHub and GitLab tokens are present, sync_followup runs both."""

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        overlay = SyncOverlay(
            github_token="gh-token",
            github_owner="org",
            github_project_number=1,
            gitlab_token="gl-token",
        )
        with _patch_overlay(overlay):
            yield

    def test_runs_both_syncs(self) -> None:
        """Both GitHub and GitLab results are merged into one SyncResult."""
        github_item = MagicMock()
        github_item.url = _GITHUB_ITEM_URL
        github_item.title = "Fix issue"
        github_item.status = "In Progress"
        github_item.position = 1
        github_item.labels = []
        github_item.updated_at = "2026-04-01T00:00:00Z"

        self._monkeypatch.setattr(
            "teatree.backends.github.fetch_project_items",
            lambda *_a, **_kw: [github_item],
        )

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_found == 2  # 1 GitHub + 1 GitLab
        assert result.tickets_created == 2
        assert result.errors == []
        assert Ticket.objects.count() == 2

    def test_gitlab_only_when_no_github_token(self) -> None:
        overlay = SyncOverlay(github_token="", gitlab_token="gl-token")
        mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab_api.GitLabAPI", lambda **_kw: mock_client)

        with _patch_overlay(overlay):
            result = sync_followup()

        assert result.prs_found == 1
        assert result.tickets_created == 1

    def test_no_tokens_returns_error(self) -> None:
        overlay = SyncOverlay(github_token="", gitlab_token="")
        with _patch_overlay(overlay):
            result = sync_followup()

        assert len(result.errors) == 1
        assert "No code host token" in result.errors[0]


class TestSyncReviewerMRs(TestCase):
    def test_caches_reviewer_mrs(self) -> None:
        from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

        mock_client = MagicMock()
        mock_client.list_open_mrs_as_reviewer.return_value = [
            {
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/99",
                "title": "Fix widget",
                "iid": 99,
                "draft": False,
                "updated_at": "2026-04-09T10:00:00Z",
                "author": {"username": "alice"},
            },
        ]
        host = GitLabCodeHost(client=mock_client)
        result = SyncResult()

        GitLabSyncBackend._sync_reviewer_prs(host, "testuser", result)

        cached = cache.get(PENDING_REVIEWS_CACHE_KEY)
        assert cached is not None
        assert len(cached) == 1
        assert cached[0]["author"] == "alice"
        assert cached[0]["repo"] == "repo"
        assert result.reviews_synced == 1

        cache.delete(PENDING_REVIEWS_CACHE_KEY)

    def test_handles_api_failure_gracefully(self) -> None:
        from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415

        mock_client = MagicMock()
        mock_client.list_open_mrs_as_reviewer.side_effect = RuntimeError("API down")
        host = GitLabCodeHost(client=mock_client)
        result = SyncResult()

        GitLabSyncBackend._sync_reviewer_prs(host, "testuser", result)

        assert len(result.errors) == 1
        assert "Reviewer PR fetch failed" in result.errors[0]


# ── GitHub sync ──────────────────────────────────────────────────────


class TestSyncGitHub(TestCase):
    def _make_overlay(self, **kwargs: object) -> SyncOverlay:
        return SyncOverlay(
            gitlab_token="",
            gitlab_username="",
            github_token="gh-test-token",
            github_owner="souliane",
            github_project_number=1,
            **kwargs,
        )

    def test_creates_ticket_from_project_item(self) -> None:
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        overlay = self._make_overlay()
        item = ProjectItem(
            issue_number=42,
            title="Test issue",
            url="https://github.com/souliane/teatree/issues/42",
            status="In Progress",
            position=1,
            labels=["bug"],
            updated_at="2026-04-01T00:00:00Z",
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
        ):
            result = GitHubSyncBackend().sync(overlay)

        assert result.tickets_created == 1
        assert result.prs_found == 1
        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/42")
        assert ticket.state == Ticket.State.STARTED
        assert ticket.extra["issue_title"] == "Test issue"

    def test_updates_existing_ticket(self) -> None:
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        overlay = self._make_overlay()
        Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/43",
            state=Ticket.State.NOT_STARTED,
            extra={"custom_key": "preserved"},
        )

        item = ProjectItem(
            issue_number=43,
            title="Updated issue",
            url="https://github.com/souliane/teatree/issues/43",
            status="Done",
            position=2,
            labels=["enhancement"],
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github_sync.cleanup_worktree"),
        ):
            result = GitHubSyncBackend().sync(overlay)

        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/43")
        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.extra["custom_key"] == "preserved"
        assert ticket.extra["issue_title"] == "Updated issue"

    def test_auto_cleans_worktrees_on_board_done_transition(self) -> None:
        """Board moves ticket to ``Done`` → cleanup runs for each worktree."""
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        overlay = self._make_overlay()
        ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/44",
            state=Ticket.State.IN_REVIEW,
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="souliane/teatree",
            branch="fix-44",
        )
        item = ProjectItem(
            issue_number=44,
            title="Ready for cleanup",
            url="https://github.com/souliane/teatree/issues/44",
            status="Done",
            position=3,
            labels=[],
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github_sync.cleanup_worktree") as mock_cleanup,
        ):
            result = GitHubSyncBackend().sync(overlay)

        mock_cleanup.assert_called_once()
        assert result.worktrees_cleaned == 1

    def test_skips_cleanup_when_ticket_already_delivered(self) -> None:
        """Ticket already Done on a prior sync → no double cleanup."""
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        overlay = self._make_overlay()
        Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/45",
            state=Ticket.State.DELIVERED,
        )
        item = ProjectItem(
            issue_number=45,
            title="Already delivered",
            url="https://github.com/souliane/teatree/issues/45",
            status="Done",
            position=4,
            labels=[],
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github_sync.cleanup_worktree") as mock_cleanup,
        ):
            GitHubSyncBackend().sync(overlay)

        mock_cleanup.assert_not_called()

    def test_keeps_worktree_with_unpushed_work(self) -> None:
        """RuntimeError from cleanup_worktree (unsynced commits) doesn't add to errors — logged as info."""
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        overlay = self._make_overlay()
        ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/46",
            state=Ticket.State.IN_REVIEW,
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="souliane/teatree",
            branch="fix-46",
        )
        item = ProjectItem(
            issue_number=46,
            title="Has unpushed work",
            url="https://github.com/souliane/teatree/issues/46",
            status="Done",
            position=5,
            labels=[],
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch(
                "teatree.backends.github_sync.cleanup_worktree",
                side_effect=RuntimeError("refused cleanup — 1 unsynced commit(s)"),
            ),
        ):
            result = GitHubSyncBackend().sync(overlay)

        assert result.worktrees_cleaned == 0
        assert result.errors == []  # info-level keep, not error

    def test_returns_error_for_non_overlay(self) -> None:
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        result = GitHubSyncBackend().sync("not an overlay")
        assert any("Invalid overlay" in e for e in result.errors)

    def test_returns_error_when_config_missing(self) -> None:
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        overlay = SyncOverlay(
            gitlab_token="",
            gitlab_username="",
            github_token="gh-token",
            github_owner="",
            github_project_number=0,
        )

        with _patch_overlay(overlay):
            result = GitHubSyncBackend().sync(overlay)

        assert any("not configured" in e for e in result.errors)

    def test_handles_fetch_exception(self) -> None:
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        overlay = self._make_overlay()

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", side_effect=RuntimeError("API error")),
        ):
            result = GitHubSyncBackend().sync(overlay)

        assert any("fetch failed" in e for e in result.errors)


class TestSyncGitHubReviewerPrs(TestCase):
    def test_caches_reviewer_prs(self) -> None:
        import json  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        prs = [
            {
                "url": "https://github.com/org/repo/pull/10",
                "title": "Fix bug",
                "repository": {"name": "repo"},
                "number": 10,
                "author": {"login": "alice"},
                "isDraft": False,
                "updatedAt": "2026-04-01T00:00:00Z",
            },
        ]
        mock_run = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(prs)),
        )
        result = SyncResult()

        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", mock_run),
        ):
            GitHubSyncBackend._sync_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 1
        cached = cache.get(PENDING_REVIEWS_CACHE_KEY)
        assert cached is not None
        assert len(cached) == 1
        assert cached[0]["author"] == "alice"
        cache.delete(PENDING_REVIEWS_CACHE_KEY)

    def test_skips_when_gh_not_found(self) -> None:
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        result = SyncResult()
        with patch("shutil.which", return_value=None):
            GitHubSyncBackend._sync_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 0

    def test_handles_subprocess_exception(self) -> None:
        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        result = SyncResult()
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", side_effect=OSError("spawn failed")),
        ):
            GitHubSyncBackend._sync_reviewer_prs("gh-token", result)

        assert any("reviewer PR fetch failed" in e for e in result.errors)

    def test_returns_early_on_nonzero_exit(self) -> None:
        import subprocess  # noqa: PLC0415

        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        result = SyncResult()
        mock_run = MagicMock(
            return_value=subprocess.CompletedProcess([], 1, stdout=""),
        )
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", mock_run),
        ):
            GitHubSyncBackend._sync_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 0

    def test_handles_invalid_json(self) -> None:
        import subprocess  # noqa: PLC0415

        from teatree.backends.github_sync import GitHubSyncBackend  # noqa: PLC0415

        result = SyncResult()
        mock_run = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="not json"),
        )
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", mock_run),
        ):
            GitHubSyncBackend._sync_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 0
