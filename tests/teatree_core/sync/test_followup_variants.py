"""sync_followup variant tests (souliane/teatree#443 split of test_sync.py).

Covers sync_followup for assigned issues, work items, merged MRs and labels.
"""

from collections.abc import Iterator

import httpx
import pytest
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.sync import sync_followup
from tests.teatree_core.sync._overlays import (
    _ASSIGNED_ISSUE,
    _ASSIGNED_WORK_ITEM,
    _CLOSED_MR,
    _MERGED_MR,
    _MR_WITH_ISSUE,
    _MR_WITH_WORK_ITEM,
    SyncOverlay,
    _make_closed_mock,
    _make_merged_mock,
    _make_mock_client,
    _patch_overlay,
)


class TestSyncFollowupAssignedIssues(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_creates_ticket_from_assigned_issue_without_mr(self) -> None:
        mock_client = _make_mock_client([])
        mock_client.list_open_issues_for_assignee.return_value = [_ASSIGNED_ISSUE]
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.issues_found == 1
        assert result.tickets_created == 1
        ticket = Ticket.objects.get(issue_url=_ASSIGNED_ISSUE["web_url"])
        assert ticket.state == Ticket.State.NOT_STARTED
        assert ticket.repos == ["repo"]
        # issue_title starts from the list response then gets refreshed by _fetch_issue_labels
        assert ticket.extra["issue_title"]
        assert "prs" not in ticket.extra or ticket.extra["prs"] == {}

    def test_skips_duplicate_when_mr_already_created_ticket(self) -> None:
        mock_client = _make_mock_client([_MR_WITH_ISSUE])
        mock_client.list_open_issues_for_assignee.return_value = [
            {
                "web_url": "https://gitlab.com/org/repo/-/issues/100",
                "title": "Issue title",
                "state": "opened",
            },
        ]
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        tickets = Ticket.objects.filter(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert tickets.count() == 1
        assert _MR_WITH_ISSUE["web_url"] in tickets.first().extra["prs"]

    def test_captures_api_errors(self) -> None:
        mock_client = _make_mock_client([])
        mock_client.list_open_issues_for_assignee.side_effect = httpx.RequestError("boom")
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert any("Assigned issues fetch failed: boom" in e for e in result.errors)
        assert result.issues_found == 0

    def test_creates_work_item_ticket(self) -> None:
        mock_client = _make_mock_client([])
        mock_client.list_open_issues_for_assignee.return_value = [_ASSIGNED_WORK_ITEM]
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url=_ASSIGNED_WORK_ITEM["web_url"])
        assert ticket.repos == ["repo"]

    def test_skips_entries_missing_url(self) -> None:
        mock_client = _make_mock_client([])
        mock_client.list_open_issues_for_assignee.return_value = [{"title": "no url"}]
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.tickets_created == 0
        assert result.issues_found == 0


class TestSyncFollowupWorkItems(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_fetches_work_item_status(self) -> None:
        """Work items without Process:: labels get their status from the GraphQL Status widget."""
        mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
        mock_client.get_issue.return_value = {"labels": [], "title": "Work item title"}
        mock_client.get_work_item_status.return_value = "In progress"
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
        assert ticket.extra["tracker_status"] == "In progress"
        assert ticket.extra["issue_title"] == "Work item title"

    def test_process_label_takes_precedence(self) -> None:
        """When a work item has Process:: labels, those take precedence over the Status widget."""
        mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
        mock_client.get_issue.return_value = {"labels": ["Process::Doing"], "title": "Work item title"}
        mock_client.get_work_item_status.return_value = "In progress"
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
        # Process:: label wins - GraphQL should NOT have been called
        assert ticket.extra["tracker_status"] == "Process::Doing"
        mock_client.get_work_item_status.assert_not_called()

    def test_status_none_falls_through(self) -> None:
        """When GraphQL returns no status, tracker_status stays empty."""
        mock_client = _make_mock_client([_MR_WITH_WORK_ITEM])
        mock_client.get_issue.return_value = {"labels": [], "title": "Work item title"}
        mock_client.get_work_item_status.return_value = None
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/work_items/200")
        assert "tracker_status" not in ticket.extra


class TestSyncFollowupMergedMrs(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_removes_discussions_from_merged_mr(self) -> None:
        """When an MR is merged, its discussions should be removed from the ticket."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        "discussions": [
                            {"status": "addressed", "detail": "Nit: simplify dict comp"},
                            {"status": "addressed", "detail": "import order"},
                        ],
                    },
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_merged == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        mr = ticket.extra["prs"]["https://gitlab.com/org/repo/-/merge_requests/42"]
        assert "discussions" not in mr

    def test_advances_ticket_to_merged_when_all_prs_merged(self) -> None:
        """When all MRs for a ticket are merged, ticket state advances to MERGED."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        "discussions": [{"status": "addressed", "detail": "nit"}],
                    },
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.MERGED

    def test_does_not_advance_when_some_mrs_still_open(self) -> None:
        """Ticket should stay in current state if only some MRs are merged."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        "discussions": [{"status": "addressed", "detail": "nit"}],
                    },
                    "https://gitlab.com/org/repo/-/merge_requests/99": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/99",
                        "repo": "repo",
                        "iid": 99,
                        "discussions": [{"status": "needs_reply", "detail": "fix this"}],
                    },
                },
            },
        )

        # Only MR 42 is merged; MR 99 is still open
        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/100")
        assert ticket.state == Ticket.State.IN_REVIEW
        # Merged MR's discussions removed, open MR's discussions preserved
        assert "discussions" not in ticket.extra["prs"]["https://gitlab.com/org/repo/-/merge_requests/42"]
        assert "discussions" in ticket.extra["prs"]["https://gitlab.com/org/repo/-/merge_requests/99"]

    def test_handles_merged_mr_fetch_failure(self) -> None:
        """When merged MR fetch fails, error is appended but sync continues."""
        mock_client = _make_mock_client([])
        mock_client.list_recently_merged_mrs.side_effect = httpx.ConnectError("timeout")
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert any("Merged PR fetch failed" in e for e in result.errors)

    def test_skips_ticket_with_no_mrs(self) -> None:
        """Ticket with empty/missing mrs dict should be skipped in merged detection."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/300",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {}},
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_merged == 0

    def test_skips_non_dict_mr_entry(self) -> None:
        """Non-dict mr_entry values should be skipped in merged detection."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/301",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": "not-a-dict",
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        # Non-dict entries are skipped; no crash, no merge count
        assert result.prs_merged == 0

    def test_followup_marks_closed_mr_so_dashboard_filters_it(self) -> None:
        """When user closes an MR without merging, sync must update the cached state to "closed".

        Regression: previously sync only fetched opened+merged states, so closed MRs kept
        cached state="opened" forever and the dashboard kept rendering them.
        """
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/77",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    _CLOSED_MR["web_url"]: {
                        "url": _CLOSED_MR["web_url"],
                        "repo": "repo",
                        "iid": 77,
                        "state": "opened",
                    },
                },
            },
        )

        mock_client = _make_closed_mock([_CLOSED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.prs_closed == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/77")
        assert ticket.extra["prs"][_CLOSED_MR["web_url"]]["state"] == "closed"

    def test_handles_closed_mr_fetch_failure(self) -> None:
        """Closed-MR fetch failure logs an error but does not abort sync."""
        mock_client = _make_mock_client([])
        mock_client.list_recently_closed_mrs.side_effect = httpx.ConnectError("timeout")
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert any("Closed PR fetch failed" in e for e in result.errors)

    def test_no_change_when_mr_has_no_discussions(self) -> None:
        """Merged MR without discussions causes no save (no changed flag)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/302",
            repos=["repo"],
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/42": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/42",
                        "repo": "repo",
                        "iid": 42,
                        # No "discussions" key
                    },
                },
            },
        )

        mock_client = _make_merged_mock([_MERGED_MR])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        # MR is counted as merged even without discussions
        assert result.prs_merged == 1


class TestSyncFollowupLabels(TestCase):
    _OVERLAY = SyncOverlay()

    @pytest.fixture(autouse=True)
    def _with_overlay(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        self._monkeypatch = monkeypatch
        with _patch_overlay(self._OVERLAY):
            yield

    def test_skips_issue_url_with_no_regex_match(self) -> None:
        """Issue URLs not matching the gitlab pattern are skipped."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/weird-url/-/issues/999",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.resolve_project.return_value = None  # Force no project
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_skips_iid_zero(self) -> None:
        """Issue with iid 0 should be skipped."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/0",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_skips_when_project_not_resolved(self) -> None:
        """When resolve_project returns None, skip the ticket."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/50",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.resolve_project.return_value = None
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_skips_when_issue_not_found(self) -> None:
        """When get_issue returns None, skip the ticket."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/50",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.get_issue.return_value = None
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_no_change_when_labels_and_title_unchanged(self) -> None:
        """When tracker_status and issue_title are already the same, no save happens."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/50",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={"tracker_status": "Process::Doing", "issue_title": "Issue title"},
        )

        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        # No change -> labels_fetched stays at 0
        assert result.labels_fetched == 0

    def test_skips_non_gitlab_url(self) -> None:
        """Issue URL without GitLab /-/ pattern should not match the regex."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/org/repo/issues/5",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        result = sync_followup()

        assert result.labels_fetched == 0

    def test_updates_ticket_variant_from_issue_labels(self) -> None:
        """_fetch_issue_labels extracts variant from labels and saves to ticket (lines 323-326)."""
        overlay = SyncOverlay(known_variants=["Acme", "BigCorp"])
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/600",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={},
        )

        mock_client = _make_mock_client([])
        mock_client.get_issue.return_value = {"labels": ["acme", "Bug"], "title": "Fix bug"}
        self._monkeypatch.setattr("teatree.backends.gitlab.api.GitLabAPI", lambda **_kw: mock_client)

        with _patch_overlay(overlay):
            sync_followup()

        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/600")
        assert ticket.variant == "Acme"
