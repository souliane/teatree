"""GitHub-sync, dual-sync and reviewer-PR tests (souliane/teatree#443 split of test_sync.py).

Covers _merge_results, dual GitHub+GitLab sync, reviewer MR/PR caching and
the GitHub sync backend.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import TestCase

from teatree.backends.github import ProjectItem
from teatree.backends.github.sync import GitHubSyncBackend
from teatree.backends.gitlab.sync import GitLabSyncBackend
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
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_found == 2  # 1 GitHub + 1 GitLab
        assert result.tickets_created == 2
        assert result.errors == []
        assert Ticket.objects.count() == 2

    def test_gitlab_only_when_no_github_token(self) -> None:
        overlay = SyncOverlay(github_token="", gitlab_token="gl-token")
        mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

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


class TestSyncClassifiesTicketKind(TestCase):
    """#17: the GitHub board-sync intake classifies Ticket.kind at create time.

    Before the wire-up a board issue labeled ``bug`` (or titled ``fix …``) synced
    as FEATURE forever — classification is create-only, so S2 stayed blind and the
    fix-record DoD gate never fired for real board defects. RED before the create
    passed ``kind=`` through (the ticket defaulted to FEATURE).
    """

    def _overlay(self) -> SyncOverlay:
        return SyncOverlay(
            gitlab_token="",
            gitlab_username="",
            github_token="gh-test-token",
            github_owner="souliane",
            github_project_number=1,
        )

    def _synced_ticket(self, *, url: str, title: str, labels: list[str]) -> Ticket:
        overlay = self._overlay()
        item = ProjectItem(
            issue_number=71,
            title=title,
            url=url,
            status="Todo",
            position=0,
            labels=labels,
            updated_at="2026-04-01T00:00:00Z",
        )
        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
        ):
            GitHubSyncBackend().sync(overlay)
        return Ticket.objects.get(issue_url=url)

    def test_bug_labeled_board_issue_is_fix(self) -> None:
        ticket = self._synced_ticket(
            url="https://github.com/souliane/teatree/issues/711",
            title="Login button unresponsive",
            labels=["bug"],
        )
        assert ticket.kind == Ticket.Kind.FIX

    def test_fix_titled_board_issue_is_fix(self) -> None:
        ticket = self._synced_ticket(
            url="https://github.com/souliane/teatree/issues/712",
            title="fix: crash on empty export",
            labels=[],
        )
        assert ticket.kind == Ticket.Kind.FIX

    def test_plain_feature_board_issue_is_feature(self) -> None:
        ticket = self._synced_ticket(
            url="https://github.com/souliane/teatree/issues/713",
            title="Add dark mode toggle",
            labels=["enhancement"],
        )
        assert ticket.kind == Ticket.Kind.FEATURE


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
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
            patch("teatree.backends.github.sync.cleanup_worktree"),
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
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
            patch("teatree.backends.github.sync.cleanup_worktree") as mock_cleanup,
        ):
            result = GitHubSyncBackend().sync(overlay)

        mock_cleanup.assert_called_once()
        assert result.worktrees_cleaned == 1

    def test_skips_cleanup_when_ticket_already_delivered(self) -> None:
        """Ticket already Done on a prior sync → no double cleanup."""
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
            patch("teatree.backends.github.sync.cleanup_worktree") as mock_cleanup,
        ):
            GitHubSyncBackend().sync(overlay)

        mock_cleanup.assert_not_called()

    def test_keeps_worktree_with_unpushed_work(self) -> None:
        """RuntimeError from cleanup_worktree (unsynced commits) doesn't add to errors — logged as info."""
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
                "teatree.backends.github.sync.cleanup_worktree",
                side_effect=RuntimeError("refused cleanup — 1 unsynced commit(s)"),
            ),
        ):
            result = GitHubSyncBackend().sync(overlay)

        assert result.worktrees_cleaned == 0
        assert result.errors == []  # info-level keep, not error

    def test_cleanup_step_errors_propagate_into_sync_result_errors(self) -> None:
        """#877 — a non-clean ``CleanupResult`` surfaces in ``SyncResult.errors``.

        The cleanup completes (worktree row gone) but a side resource failed.
        Before #877 that failure was swallowed into a label string the sync
        backend never inspected (#932); now it reaches the operator via the
        ``SyncResult.errors`` exit channel.
        """
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415
        from teatree.core.cleanup import CleanupResult  # noqa: PLC0415

        overlay = self._make_overlay()
        ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/47",
            state=Ticket.State.IN_REVIEW,
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="souliane/teatree",
            branch="fix-47",
        )
        item = ProjectItem(
            issue_number=47,
            title="Cleanup with a failing side resource",
            url="https://github.com/souliane/teatree/issues/47",
            status="Done",
            position=6,
            labels=[],
        )
        dirty_result = CleanupResult(
            label="Cleaned: souliane/teatree (fix-47)",
            errors=["dropdb failed for wt_47: connection refused"],
        )

        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github.sync.cleanup_worktree", return_value=dirty_result),
        ):
            result = GitHubSyncBackend().sync(overlay)

        assert result.worktrees_cleaned == 1
        assert any("dropdb failed for wt_47" in e for e in result.errors)

    def test_done_board_move_keeps_delivered_but_records_dod_violation(self) -> None:
        """#1426: board 'Done' -> DELIVERED is terminal reality (kept), with an audit marker.

        A UI-visible ticket with no local E2E keeps DELIVERED but gets a durable
        dod_e2e_violation marker recorded.
        """
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415
        from teatree.core.gates import dod_gate  # noqa: PLC0415

        overlay = self._make_overlay()
        Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/48",
            state=Ticket.State.IN_REVIEW,
            repos=["teatree"],
        )
        item = ProjectItem(
            issue_number=48,
            title="Delivered without local E2E",
            url="https://github.com/souliane/teatree/issues/48",
            status="Done",
            position=7,
            labels=[],
        )
        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github.sync.cleanup_worktree"),
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=["teatree"]),
        ):
            GitHubSyncBackend().sync(overlay)

        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/48")
        assert ticket.state == Ticket.State.DELIVERED  # terminal reality kept
        assert ticket.extra["dod_e2e_violation"]["state"] == Ticket.State.DELIVERED

    def test_done_board_move_no_violation_when_not_ui_visible(self) -> None:
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415
        from teatree.core.gates import dod_gate  # noqa: PLC0415

        overlay = self._make_overlay()
        Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/49",
            state=Ticket.State.IN_REVIEW,
            repos=["teatree"],
        )
        item = ProjectItem(
            issue_number=49,
            title="Backend-only delivered",
            url="https://github.com/souliane/teatree/issues/49",
            status="Done",
            position=8,
            labels=[],
        )
        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github.sync.cleanup_worktree"),
            # ticket repo "teatree" not in the overlay's frontend repos -> not UI-visible
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=["some-frontend"]),
        ):
            GitHubSyncBackend().sync(overlay)

        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/49")
        assert ticket.state == Ticket.State.DELIVERED
        assert "dod_e2e_violation" not in ticket.extra

    def test_newly_created_done_item_scopes_repo_from_url_and_records_violation(self) -> None:
        """#1426: a board-created 'Done' ticket scopes repos from the issue URL.

        Scoping from the URL (not the project owner) lets the DoD gate see the
        real repo and record the violation.
        """
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415
        from teatree.core.gates import dod_gate  # noqa: PLC0415

        overlay = self._make_overlay()  # github_owner="souliane"
        item = ProjectItem(
            issue_number=50,
            title="Delivered via board, never seen before",
            url="https://github.com/souliane/teatree/issues/50",
            status="Done",
            position=9,
            labels=[],
        )
        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github.sync.cleanup_worktree"),
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=["teatree"]),
        ):
            GitHubSyncBackend().sync(overlay)

        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/50")
        # Repo scoped from the URL ("teatree"), NOT the project owner ("souliane").
        assert ticket.repos == ["teatree"]
        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.extra["dod_e2e_violation"]["state"] == Ticket.State.DELIVERED

    def test_already_delivered_ticket_gets_violation_on_resync_after_repo_repair(self) -> None:
        """#1426: a stuck-DELIVERED ticket gets the audit on re-sync after repo repair.

        A ticket the old owner-scoping bug left at DELIVERED with no marker
        gets the audit once its repo scope is repaired from the URL.
        """
        from teatree.backends.github import ProjectItem  # noqa: PLC0415
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415
        from teatree.core.gates import dod_gate  # noqa: PLC0415

        overlay = self._make_overlay()
        # Pre-existing ticket: already DELIVERED, mis-scoped to the owner slug,
        # and crucially missing the dod_e2e_violation marker.
        Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/51",
            state=Ticket.State.DELIVERED,
            repos=["souliane"],
        )
        item = ProjectItem(
            issue_number=51,
            title="Stuck delivered, no marker",
            url="https://github.com/souliane/teatree/issues/51",
            status="Done",
            position=10,
            labels=[],
        )
        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github.sync.cleanup_worktree") as mock_cleanup,
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=["teatree"]),
        ):
            GitHubSyncBackend().sync(overlay)

        ticket = Ticket.objects.get(issue_url="https://github.com/souliane/teatree/issues/51")
        assert "teatree" in ticket.repos  # repo scope repaired from the URL
        assert ticket.extra["dod_e2e_violation"]["state"] == Ticket.State.DELIVERED
        # No transition happened (already DELIVERED) so cleanup must not re-run.
        mock_cleanup.assert_not_called()

    def test_returns_error_for_non_overlay(self) -> None:
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

        result = GitHubSyncBackend().sync("not an overlay")
        assert any("Invalid overlay" in e for e in result.errors)

    def test_returns_error_when_config_missing(self) -> None:
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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

        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

        result = SyncResult()
        with patch("shutil.which", return_value=None):
            GitHubSyncBackend._sync_reviewer_prs("gh-token", result)

        assert result.reviews_synced == 0

    def test_handles_subprocess_exception(self) -> None:
        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

        result = SyncResult()
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", side_effect=OSError("spawn failed")),
        ):
            GitHubSyncBackend._sync_reviewer_prs("gh-token", result)

        assert any("reviewer PR fetch failed" in e for e in result.errors)

    def test_returns_early_on_nonzero_exit(self) -> None:
        import subprocess  # noqa: PLC0415

        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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

        from teatree.backends.github.sync import GitHubSyncBackend  # noqa: PLC0415

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
