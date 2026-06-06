"""Core sync_followup tests (souliane/teatree#443 split of test_sync.py).

Covers the main sync_followup ticket-creation/update flow against GitLab.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.core.cache import cache
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.sync import sync_followup
from teatree.types import LAST_SYNC_CACHE_KEY
from tests.teatree_core.sync._overlays import (
    _MR_WITH_ISSUE,
    _MR_WITHOUT_ISSUE,
    SyncOverlay,
    _make_mock_client,
    _patch_overlay,
)


class TestSyncFollowup(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_creates_tickets_from_mrs(self) -> None:
        mock_client = _make_mock_client([_MR_WITH_ISSUE, _MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_found == 2
        assert result.tickets_created == 2
        assert result.errors == []
        assert Ticket.objects.count() == 2

        issue_ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert "repo" in issue_ticket.repos
        assert "prs" in issue_ticket.extra
        # Non-draft MR should have pipeline data
        mr_data = issue_ticket.extra["prs"][_MR_WITH_ISSUE["web_url"]]
        assert mr_data["pipeline_status"] == "success"
        assert mr_data["approvals"] == {"count": 0, "required": 1}

        mr_ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
        assert mr_ticket.extra["prs"][_MR_WITHOUT_ISSUE["web_url"]]["draft"] is True

    def test_sync_sets_draft_comments_pending(self) -> None:
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        mock_client.get_draft_notes_count.return_value = 5
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        mr_data = ticket.extra["prs"][_MR_WITH_ISSUE["web_url"]]
        assert mr_data["draft_comments_pending"] is True
        assert mr_data["draft_comments_count"] == 5

    def test_sync_clears_draft_comments_when_zero(self) -> None:
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        mock_client.get_draft_notes_count.return_value = 0
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        mr_data = ticket.extra["prs"][_MR_WITH_ISSUE["web_url"]]
        assert mr_data["draft_comments_pending"] is False
        assert "draft_comments_count" not in mr_data

    def test_fetches_issue_labels(self) -> None:
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.extra["tracker_status"] == "Process::Doing"
        assert ticket.extra["issue_title"] == "Issue title"

    def test_updates_existing_ticket(self) -> None:
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["old-repo"],
            extra={"prs": {}},
        )

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_created == 0
        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert "repo" in ticket.repos
        assert "old-repo" in ticket.repos
        assert _MR_WITH_ISSUE["web_url"] in ticket.extra["prs"]

    def test_returns_error_when_token_missing(self) -> None:
        overlay = SyncOverlay(gitlab_token="")
        with _patch_overlay(overlay):
            result = sync_followup()

        assert len(result.errors) == 1
        assert "No code host token for" in result.errors[0]

    def test_captures_api_errors(self) -> None:
        mock_client = MagicMock()
        mock_client.list_all_open_mrs.side_effect = RuntimeError("API timeout")
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_found == 0
        assert len(result.errors) == 1
        assert "API timeout" in result.errors[0]

    def test_unhandled_backend_exception_does_not_crash_sync(self) -> None:
        """A backend raising an unhandled exception must surface as a SyncResult error.

        Regression: ``httpx.ReadTimeout`` from a backend internal call escaped the
        backend's own try/except and crashed ``sync_followup``, which surfaced as
        an HTTP 500 on ``/dashboard/sync/``. The dashboard must receive a
        SyncResult with errors instead.
        """
        failing_backend = MagicMock()
        failing_backend.is_configured.return_value = True
        failing_backend.sync.side_effect = httpx.ReadTimeout("upstream hung")
        type(failing_backend).__name__ = "FailingBackend"

        with (
            patch("teatree.backends.gitlab.sync.GitLabSyncBackend", return_value=failing_backend),
            patch("teatree.backends.github.sync.GitHubSyncBackend") as gh_backend_cls,
        ):
            gh_backend_cls.return_value.is_configured.return_value = False
            result = sync_followup()

        assert result.prs_found == 0
        assert any("upstream hung" in e for e in result.errors)
        assert any("FailingBackend" in e for e in result.errors)

    def test_returns_error_when_username_missing(self) -> None:
        overlay = SyncOverlay(gitlab_username="")
        mock_client = MagicMock()
        mock_client.current_username.return_value = ""
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        with _patch_overlay(overlay):
            result = sync_followup()

        assert result.errors == ["GitLab username is not configured in overlay"]

    def test_updates_existing_mr_only_ticket(self) -> None:
        Ticket.objects.create(
            overlay="test",
            issue_url=_MR_WITHOUT_ISSUE["web_url"],
            repos=["repo"],
            extra={"prs": {}},
        )

        mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
        assert _MR_WITHOUT_ISSUE["web_url"] in ticket.extra["prs"]

    def test_handles_corrupted_extra_field(self) -> None:
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            extra={"prs": "not-a-dict"},
        )

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_updated == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert isinstance(ticket.extra["prs"], dict)

    def test_first_run_passes_no_updated_after(self) -> None:
        """First sync (no cached timestamp) should call list_open_mrs without updated_after."""
        cache.delete(LAST_SYNC_CACHE_KEY)
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        mock_client.list_all_open_mrs.assert_called_once_with("testuser", updated_after=None)

    def test_stores_timestamp_and_uses_it_on_next_run(self) -> None:
        """After a successful sync, the timestamp is cached and passed on the next call."""
        cache.delete(LAST_SYNC_CACHE_KEY)
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        # First run: stores the timestamp
        sync_followup()
        stored = cache.get(LAST_SYNC_CACHE_KEY)
        assert stored is not None

        # Second run: should pass the stored timestamp as updated_after
        mock_client.reset_mock()
        mock_client.list_all_open_mrs.return_value = []
        sync_followup()

        mock_client.list_all_open_mrs.assert_called_once_with("testuser", updated_after=stored)

    def test_stores_timestamp_even_when_no_mrs_returned(self) -> None:
        """Timestamp is stored after a successful sync even if zero MRs are returned."""
        cache.delete(LAST_SYNC_CACHE_KEY)
        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        assert cache.get(LAST_SYNC_CACHE_KEY) is not None

    def test_creates_ticket_with_inferred_state(self) -> None:
        """New ticket from a non-draft MR should be SHIPPED, not NOT_STARTED."""
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.SHIPPED

    def test_creates_draft_ticket_as_started(self) -> None:
        """New ticket from a draft MR should be STARTED."""
        mock_client = _make_mock_client([_MR_WITHOUT_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url=_MR_WITHOUT_ISSUE["web_url"])
        assert ticket.state == Ticket.State.STARTED

    def test_advances_existing_ticket_state(self) -> None:
        """Existing ticket at NOT_STARTED should advance when MR data implies a later state."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.NOT_STARTED,
            extra={"prs": {}},
        )
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.SHIPPED

    def test_does_not_regress_ticket_state(self) -> None:
        """Ticket already at IN_REVIEW should not regress to SHIPPED on sync."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {}},
        )
        # MR with no approvals -> inferred SHIPPED, but ticket is already at IN_REVIEW
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        mock_client.get_mr_approvals.return_value = {"count": 0, "required": 1}
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_handles_non_list_reviewers(self) -> None:
        """When reviewers is not a list (e.g. None), reviewer fields are omitted."""
        mr = {
            **_MR_WITHOUT_ISSUE,
            "reviewers": None,
        }
        mock_client = _make_mock_client([mr])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_created == 1
        ticket = Ticket.objects.get(issue_url=mr["web_url"])
        mr_data = ticket.extra["prs"][mr["web_url"]]
        assert "review_requested" not in mr_data
        assert "reviewer_names" not in mr_data

    def test_deduplicates_tickets_on_upsert(self) -> None:
        """When duplicate tickets exist for the same issue_url, sync merges and deletes extras."""
        ticket_a = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            extra={"prs": {"https://mr/old": {"title": "old"}}},
            state=Ticket.State.STARTED,
        )
        dup_b = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/101",
            repos=["other-repo"],
            extra={"prs": {"https://mr/dup": {"title": "dup"}}},
        )
        dup_c = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/102",
            repos=[],
            extra={},
        )

        original_filter = Ticket.objects.filter

        def patched_filter(**kwargs):
            qs = original_filter(**kwargs)
            if kwargs.get("issue_url") == "https://gitlab.com/org/repo/-/issues/100":
                return Ticket.objects.filter(pk__in=[ticket_a.pk, dup_b.pk, dup_c.pk]).order_by("pk")
            return qs

        self._monkeypatch.setattr(Ticket.objects, "filter", patched_filter)

        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_updated >= 1
        assert not Ticket.objects.filter(pk=dup_b.pk).exists()
        assert not Ticket.objects.filter(pk=dup_c.pk).exists()
        ticket_a.refresh_from_db()
        assert "https://mr/dup" in ticket_a.extra["prs"]
        assert "other-repo" in ticket_a.repos
