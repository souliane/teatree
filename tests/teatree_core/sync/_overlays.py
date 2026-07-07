"""Shared test doubles and fixtures for the sync test package.

Extracted verbatim from the former monolithic ``tests/teatree_core/test_sync.py``
(souliane/teatree#443). No behavior change: the same overlay classes, the same
``_patch_overlay`` helper, the same fixture MR payloads, and the same mock-client
factory functions, relocated so each focused test module can import them.
"""

from unittest.mock import MagicMock, patch

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.backends.gitlab.api import ProjectInfo
from teatree.core.overlay import OverlayBase, OverlayConfig, ProvisionStep

# Test overlay classes
# ---------------------------------------------------------------------------


class SyncConfig(OverlayConfig):
    """Configurable overlay config for sync tests."""

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def __init__(  # noqa: PLR0913
        self,
        *,
        gitlab_token: str = "test-token",  # noqa: S107
        gitlab_username: str = "testuser",
        github_token: str = "",
        github_owner: str = "",
        github_project_number: int = 0,
        slack_token: str = "",
        review_channel: tuple[str, str] = ("", ""),
        known_variants: list[str] | None = None,
        frontend_repos: list[str] | None = None,
        notion_token: str = "",
        notion_status_property: str = "Status",
        notion_write_back: bool = False,
    ) -> None:
        self._gitlab_token = gitlab_token
        self._gitlab_username = gitlab_username
        self._github_token = github_token
        self.github_owner = github_owner
        self.github_project_number = github_project_number
        self._slack_token = slack_token
        self._review_channel = review_channel
        self._notion_token = notion_token
        self.notion_status_property = notion_status_property
        self.notion_write_back = notion_write_back
        self.known_variants = known_variants or []
        # Mirror the real OverlayConfig, which always exposes frontend_repos.
        # The #1426 DoD gate fails CLOSED on a config that omits it; this test
        # double must therefore declare it (empty = no UI-visible tickets).
        self.frontend_repos = frontend_repos or []

    def get_gitlab_token(self) -> str:
        return self._gitlab_token

    def get_github_token(self) -> str:
        return self._github_token

    def get_gitlab_username(self) -> str:
        return self._gitlab_username

    def get_slack_token(self) -> str:
        return self._slack_token

    def get_notion_token(self) -> str:
        return self._notion_token

    def get_review_channel(self) -> tuple[str, str]:
        return self._review_channel


class SyncOverlay(OverlayBase):
    """Overlay for sync tests with configurable GitLab/Slack/variant settings."""

    def __init__(self, **config_kwargs: object) -> None:
        self.config = SyncConfig(**config_kwargs)

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: object) -> list[ProvisionStep]:
        return []


def _patch_overlay(overlay: OverlayBase, *, name: str = "test"):
    """Return a ``patch`` that makes the overlay loader return the given instance."""
    result: dict[str, OverlayBase] = {name: overlay}

    def _fake_discover() -> dict[str, OverlayBase]:
        return result

    _fake_discover.cache_clear = lambda: None

    return patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover)


_PROJECT = ProjectInfo(project_id=123, path_with_namespace="org/repo", short_name="repo")

_MR_WITH_ISSUE = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "title": "feat: add feature",
    "description": "feat: add feature [none] (https://gitlab.com/org/repo/-/issues/100)\n\nBody",
    "source_branch": "feat/add-feature",
    "draft": False,
    "iid": 42,
    "project_id": 123,
}

_MR_WITHOUT_ISSUE = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/43",
    "title": "fix: quick patch",
    "description": "fix: quick patch",
    "source_branch": "fix/quick-patch",
    "draft": True,
    "iid": 43,
    "project_id": 123,
}

_MR_WITH_WORK_ITEM = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/44",
    "title": "feat: work item feature",
    "description": "feat: work item feature (https://gitlab.com/org/repo/-/work_items/200)\n\nBody",
    "source_branch": "feat/work-item",
    "draft": False,
    "iid": 44,
    "project_id": 123,
}

_MERGED_MR = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/42",
    "iid": 42,
    "project_id": 123,
}

_CLOSED_MR = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/77",
    "iid": 77,
    "project_id": 123,
}


def _make_mock_client(mrs: list[dict]) -> MagicMock:
    mock = MagicMock()
    mock.list_open_mrs.return_value = mrs
    mock.list_all_open_mrs.return_value = mrs
    mock.list_open_issues_for_assignee.return_value = []
    mock.list_recently_merged_mrs.return_value = []
    mock.list_recently_closed_mrs.return_value = []
    mock.resolve_project.return_value = _PROJECT
    mock.get_mr_pipeline.return_value = {"status": "success", "url": "https://gitlab.com/pipelines/1"}
    mock.get_mr_approvals.return_value = {"count": 0, "required": 1}
    mock.get_issue.return_value = {"labels": ["Process::Doing"], "title": "Issue title"}
    mock.get_draft_notes_count.return_value = 0
    return mock


def _make_merged_mock(merged_mrs: list[dict]) -> MagicMock:
    """Mock client with no open MRs and some merged MRs."""
    mock = _make_mock_client([])
    mock.list_recently_merged_mrs.return_value = merged_mrs
    return mock


def _make_closed_mock(closed_mrs: list[dict]) -> MagicMock:
    """Mock client with no open MRs and some closed-without-merge MRs."""
    mock = _make_mock_client([])
    mock.list_recently_closed_mrs.return_value = closed_mrs
    return mock


_ASSIGNED_ISSUE = {
    "web_url": "https://gitlab.com/org/repo/-/issues/500",
    "title": "Some assigned task",
    "state": "opened",
}

_ASSIGNED_WORK_ITEM = {
    "web_url": "https://gitlab.com/org/repo/-/work_items/501",
    "title": "Assigned work item",
    "state": "opened",
}
