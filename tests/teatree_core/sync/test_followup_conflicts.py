"""Followup sweep surfaces the user's OWN open authored MRs in merge conflict.

The sweep already fetches the author's open MRs to upsert tickets, but never
read each MR's ``has_conflicts`` / ``merge_status`` — so conflicted MRs (e.g.
ones that re-conflict as master advances) sat invisibly. These tests pin that
``sync_followup`` now collects conflicted authored MRs into
``SyncResult.conflicted_mrs`` and that the ``followup sync`` command surfaces
them LOUDLY in its printed output. Detection only: never an auto-resolve.

Only the GitLab HTTP boundary (``GitLabAPI`` / ``list_all_open_mrs``) is
mocked; the real ``sync_followup`` → ``GitLabSyncBackend.sync`` path runs.
"""

from collections.abc import Iterator
from io import StringIO

import pytest
from django.core.management.base import OutputWrapper
from django.test import TestCase

from teatree.core.management.commands.followup import Command
from teatree.core.sync import sync_followup
from tests.teatree_core.sync._overlays import SyncOverlay, _make_mock_client, _patch_overlay

_CONFLICTED_MR = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/7649",
    "title": "feat: conflicting change",
    "description": "feat: conflicting change",
    "source_branch": "feat/conflicting",
    "draft": False,
    "iid": 7649,
    "project_id": 123,
    "has_conflicts": True,
    "merge_status": "cannot_be_merged",
}

_CLEAN_MR = {
    "web_url": "https://gitlab.com/org/repo/-/merge_requests/7700",
    "title": "feat: clean change",
    "description": "feat: clean change",
    "source_branch": "feat/clean",
    "draft": False,
    "iid": 7700,
    "project_id": 123,
    "has_conflicts": False,
    "merge_status": "can_be_merged",
}


class TestFollowupSurfacesConflictedMRs(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_sync_collects_only_the_conflicted_authored_mr(self) -> None:
        mock_client = _make_mock_client([_CONFLICTED_MR, _CLEAN_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_found == 2
        assert [c.web_url for c in result.conflicted_mrs] == [_CONFLICTED_MR["web_url"]]
        conflicted = result.conflicted_mrs[0]
        assert conflicted.iid == 7649
        assert conflicted.title == "feat: conflicting change"
        assert conflicted.repo == "repo"

    def test_command_surfaces_conflicts_loudly(self) -> None:
        mock_client = _make_mock_client([_CONFLICTED_MR, _CLEAN_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        buffer = StringIO()
        command = Command()
        command.stdout = OutputWrapper(buffer)
        summary = command.sync()
        printed = buffer.getvalue()

        assert "WARNING" in printed
        assert "conflict" in printed.lower()
        assert "7649" in printed
        assert _CONFLICTED_MR["web_url"] in printed
        # The clean MR is never named as conflicted in the loud block.
        assert "7700" not in printed
        # The machine-readable summary carries exactly the conflicted MR too.
        assert summary["conflicted_mrs"] == [
            {
                "iid": 7649,
                "repo": "repo",
                "web_url": _CONFLICTED_MR["web_url"],
                "title": "feat: conflicting change",
            },
        ]

    def test_clean_run_emits_no_conflict_warning(self) -> None:
        mock_client = _make_mock_client([_CLEAN_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        buffer = StringIO()
        command = Command()
        command.stdout = OutputWrapper(buffer)
        summary = command.sync()

        assert "WARNING" not in buffer.getvalue()
        assert summary["conflicted_mrs"] == []
