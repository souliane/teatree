"""Review-permalink, Notion-status and tracker-404 tests (souliane/teatree#443 split of test_sync.py).

Covers fetch_review_permalinks, fetch_notion_statuses, _overlay_name,
resolve_issue 404 handling, tracker-404 memoization and detect_e2e_test_plan.
"""

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from django.test import TestCase

import teatree.core.sync as sync_mod
from teatree.backends.gitlab.api import ProjectInfo
from teatree.backends.gitlab.sync_issues import fetch_issue_labels, resolve_issue
from teatree.backends.gitlab.sync_prs import detect_e2e_test_plan
from teatree.backends.slack.review_sync import fetch_review_permalinks
from teatree.core.models import Ticket
from teatree.core.sync import _overlay_name, fetch_notion_statuses, push_notion_status, sync_followup
from teatree.types import RawAPIDict, SyncResult
from tests.teatree_core.sync._overlays import SyncOverlay, _patch_overlay

_PAGE_ID = "1a2b3c4d5e6f47a89b0c1d2e3f405162"
_NOTION_URL = f"https://www.notion.so/team/My-Ticket-{_PAGE_ID}"


def _patch_notion_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    original = httpx.Client.__init__

    def patched(self: httpx.Client, **kwargs: object) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)


class TestFetchReviewPermalinks(TestCase):
    _SLACK_OVERLAY = SyncOverlay(
        slack_token="xoxb-token",
        review_channel=("review-team", "C123"),
    )

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_returns_early_without_token(self) -> None:
        """_fetch_review_permalinks returns early when no token (line 359)."""
        overlay = SyncOverlay(slack_token="", review_channel=("", ""))
        with _patch_overlay(overlay):
            result = SyncResult()
            fetch_review_permalinks(result)
        assert result.reviews_synced == 0
        assert result.errors == []

    def test_skips_draft_mrs(self) -> None:
        """_fetch_review_permalinks skips draft MRs (line 374)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/500",
            repos=["repo"],
            state=Ticket.State.STARTED,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/50": {
                        "draft": True,
                        "url": "https://gitlab.com/org/repo/-/merge_requests/50",
                    },
                },
            },
        )

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            fetch_review_permalinks(result)
        # No non-draft MRs -> no Slack call
        assert result.reviews_synced == 0

    def test_skips_already_linked_mrs(self) -> None:
        """_fetch_review_permalinks skips MRs that already have review_permalink (line 376-377)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/501",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/51": {
                        "draft": False,
                        "review_permalink": "https://slack.com/existing",
                    },
                },
            },
        )

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            fetch_review_permalinks(result)
        assert result.reviews_synced == 0

    def test_returns_early_when_no_urls(self) -> None:
        """_fetch_review_permalinks returns early when no eligible MR URLs (line 382-383)."""
        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            fetch_review_permalinks(result)
        assert result.reviews_synced == 0

    def test_handles_search_exception(self) -> None:
        """_fetch_review_permalinks appends error on exception (line 392-393)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/502",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/52": {
                        "draft": False,
                    },
                },
            },
        )

        def _explode(request: object) -> list:
            msg = "Slack timeout"
            raise RuntimeError(msg)

        self._monkeypatch.setattr("teatree.backends.slack.review_sync.search_review_permalinks", _explode)

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            fetch_review_permalinks(result)
        assert any("Slack review sync" in e for e in result.errors)

    def test_stores_matches(self) -> None:
        """_fetch_review_permalinks updates ticket extra with permalink (lines 396-410)."""
        from teatree.backends.slack import SlackReviewMatch  # noqa: PLC0415

        mr_url = "https://gitlab.com/org/repo/-/merge_requests/53"
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/503",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={"prs": {mr_url: {"draft": False}}},
        )

        self._monkeypatch.setattr(
            "teatree.backends.slack.review_sync.search_review_permalinks",
            lambda _request: [
                SlackReviewMatch(
                    pr_url=mr_url,
                    permalink="https://team.slack.com/archives/C123/p170000",
                    channel="review-team",
                ),
            ],
        )

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            fetch_review_permalinks(result)

        assert result.reviews_synced == 1
        ticket = Ticket.objects.get(issue_url="https://gitlab.com/org/repo/-/issues/503")
        mr = ticket.extra["prs"][mr_url]
        assert mr["review_permalink"] == "https://team.slack.com/archives/C123/p170000"
        assert mr["review_channel"] == "review-team"

    def test_skips_non_dict_mrs_in_collection(self) -> None:
        """Tickets with non-dict mrs are skipped during collection (line 370)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/801",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={"prs": "not-a-dict"},
        )
        self._monkeypatch.setattr("teatree.backends.slack.review_sync.search_review_permalinks", lambda _request: [])

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            fetch_review_permalinks(result)
        assert result.reviews_synced == 0

    def test_skips_non_dict_mr_entry_in_collection(self) -> None:
        """Individual non-dict MR entries are skipped during collection (line 373)."""
        Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/802",
            repos=["repo"],
            state=Ticket.State.SHIPPED,
            extra={"prs": {"https://gitlab.com/mr/1": "not-a-dict"}},
        )
        self._monkeypatch.setattr("teatree.backends.slack.review_sync.search_review_permalinks", lambda _request: [])

        with _patch_overlay(self._SLACK_OVERLAY):
            result = SyncResult()
            fetch_review_permalinks(result)
        assert result.reviews_synced == 0


class TestFetchNotionStatuses(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_fetch_notion_statuses_hits_api_notion_com_not_connector(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"notion_url": _NOTION_URL})
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization", "")
            seen["version"] = request.headers.get("Notion-Version", "")
            return httpx.Response(200, json={"properties": {"Status": {"status": {"name": "In review"}}}})

        _patch_notion_transport(self._monkeypatch, handler)
        with _patch_overlay(SyncOverlay(notion_token="ntn_secret")):
            fetch_notion_statuses()

        assert seen["url"] == f"https://api.notion.com/v1/pages/{_PAGE_ID}"
        assert seen["auth"] == "Bearer ntn_secret"
        assert seen["version"]
        ticket.refresh_from_db()
        assert ticket.extra["notion_status"] == "In review"

    def test_fetch_notion_statuses_no_token_is_clean_noop_not_raise(self) -> None:
        ticket = Ticket.objects.create(overlay="test", extra={"notion_url": _NOTION_URL})
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"properties": {}})

        _patch_notion_transport(self._monkeypatch, handler)
        with _patch_overlay(SyncOverlay(notion_token="")):
            fetch_notion_statuses()

        assert calls == []
        ticket.refresh_from_db()
        assert "notion_status" not in ticket.extra

    def test_direct_path_taken_when_token_present_connector_branch_gone(self) -> None:
        source = Path(sync_mod.__file__).read_text(encoding="utf-8")
        assert "requires Claude MCP" not in source
        assert "NotImplementedError" not in source

        for _ in range(3):
            Ticket.objects.create(overlay="test", extra={"notion_url": _NOTION_URL})
        hits = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.notion.com"
            hits["count"] += 1
            return httpx.Response(200, json={"properties": {"Status": {"status": {"name": "Done"}}}})

        _patch_notion_transport(self._monkeypatch, handler)
        with _patch_overlay(SyncOverlay(notion_token="ntn_secret")):
            fetch_notion_statuses()

        assert hits["count"] == 3

    def test_skips_tickets_without_page_id_or_status(self) -> None:
        no_url = Ticket.objects.create(overlay="test", extra={})
        no_status = Ticket.objects.create(overlay="test", extra={"notion_url": _NOTION_URL})
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"properties": {}})

        _patch_notion_transport(self._monkeypatch, handler)
        with _patch_overlay(SyncOverlay(notion_token="ntn_secret")):
            fetch_notion_statuses()

        assert len(calls) == 1  # only the ticket carrying a notion_url is fetched
        for ticket in (no_url, no_status):
            ticket.refresh_from_db()
            assert "notion_status" not in ticket.extra

    def test_update_page_status_gated_by_write_back_flag(self) -> None:
        patches: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            patches.append(request.method)
            return httpx.Response(200, json={"object": "page", "id": _PAGE_ID})

        _patch_notion_transport(self._monkeypatch, handler)

        with _patch_overlay(SyncOverlay(notion_token="ntn_secret", notion_write_back=False)):
            assert push_notion_status(_PAGE_ID, "Merged") is False
        assert patches == []

        with _patch_overlay(SyncOverlay(notion_token="ntn_secret", notion_write_back=True)):
            assert push_notion_status(_PAGE_ID, "Merged") is True
        assert patches == ["PATCH"]

    def test_push_notion_status_write_back_on_but_no_token_is_noop(self) -> None:
        with _patch_overlay(SyncOverlay(notion_token="", notion_write_back=True)):
            assert push_notion_status(_PAGE_ID, "Merged") is False

    def test_sync_followup_surfaces_notion_error_without_aborting(self) -> None:
        def _boom() -> None:
            msg = "notion down"
            raise RuntimeError(msg)

        self._monkeypatch.setattr("teatree.core.sync.fetch_notion_statuses", _boom)
        with _patch_overlay(SyncOverlay(gitlab_token="", github_token="")):
            result = sync_followup()

        assert any("Notion status sync failed: notion down" in err for err in result.errors)


class TestOverlayName:
    def test_returns_name_for_registered_overlay(self) -> None:
        overlay = SyncOverlay()
        with _patch_overlay(overlay, name="my-overlay"):
            assert _overlay_name(overlay) == "my-overlay"

    def test_returns_empty_for_unknown_overlay(self) -> None:
        overlay = SyncOverlay()
        other = SyncOverlay()
        with _patch_overlay(other, name="other"):
            assert _overlay_name(overlay) == ""


class TestResolveIssueHandles404:
    def test_returns_none_on_404(self) -> None:
        import httpx  # noqa: PLC0415

        client = MagicMock()
        client.resolve_project.return_value = ProjectInfo(
            project_id=1,
            path_with_namespace="org/repo",
            short_name="repo",
        )
        client.get_issue.side_effect = httpx.HTTPStatusError(
            "404 Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )
        result = resolve_issue(client, "https://gitlab.com/org/repo/-/issues/123")
        assert result is None

    def test_returns_issue_on_success(self) -> None:
        client = MagicMock()
        client.resolve_project.return_value = ProjectInfo(
            project_id=1,
            path_with_namespace="org/repo",
            short_name="repo",
        )
        client.get_issue.return_value = {"id": 123, "title": "Test issue", "labels": []}
        result = resolve_issue(client, "https://gitlab.com/org/repo/-/issues/123")
        assert result is not None
        assert result[0]["title"] == "Test issue"


class TestTracker404Memoization(TestCase):
    """A 404 from GitLab persists ``tracker_404`` on the ticket so ``_fetch_issue_labels`` skips it."""

    def _client_returning_404(self) -> MagicMock:
        client = MagicMock()
        client.resolve_project.return_value = ProjectInfo(
            project_id=1,
            path_with_namespace="org/repo",
            short_name="repo",
        )
        client.get_issue.side_effect = httpx.HTTPStatusError(
            "404 Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )
        return client

    def test_resolve_issue_marks_ticket_on_404(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/123")

        result = resolve_issue(self._client_returning_404(), ticket.issue_url, ticket=ticket)

        assert result is None
        ticket.refresh_from_db()
        assert ticket.extra.get("tracker_404") is True

    def test_resolve_issue_does_not_mark_when_ticket_omitted(self) -> None:
        client = self._client_returning_404()

        result = resolve_issue(client, "https://gitlab.com/org/repo/-/issues/123")

        assert result is None  # original behavior preserved when no ticket is passed

    def test_fetch_issue_labels_skips_already_marked_tickets(self) -> None:
        marked = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/123",
            extra={"tracker_404": True},
        )
        live = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/124")
        client = MagicMock()
        client.resolve_project.return_value = ProjectInfo(
            project_id=1,
            path_with_namespace="org/repo",
            short_name="repo",
        )
        client.get_issue.return_value = {"id": 124, "title": "Live", "labels": []}

        fetch_issue_labels(client, SyncResult())

        called_iids = [call.kwargs.get("issue_iid") or call.args[1] for call in client.get_issue.call_args_list]
        assert called_iids == [124]
        marked.refresh_from_db()
        assert marked.extra.get("tracker_404") is True
        live.refresh_from_db()
        assert live.extra.get("issue_title") == "Live"


class TestDetectE2ETestPlan:
    def test_finds_evidence_with_keyword_and_image(self) -> None:
        discussions = [
            {
                "notes": [
                    {
                        "id": 42,
                        "body": "E2E test evidence:\n![screenshot](/uploads/abc.png)",
                    },
                ],
            },
        ]
        url = detect_e2e_test_plan(discussions, "https://gitlab.com/org/repo/-/merge_requests/1")
        assert url == "https://gitlab.com/org/repo/-/merge_requests/1#note_42"

    def test_skips_keyword_without_image(self) -> None:
        discussions = [{"notes": [{"id": 1, "body": "E2E tests look good"}]}]
        assert detect_e2e_test_plan(discussions, "https://example.com") == ""

    def test_skips_image_without_keyword(self) -> None:
        discussions = [{"notes": [{"id": 1, "body": "![logo](/uploads/logo.png)"}]}]
        assert detect_e2e_test_plan(discussions, "https://example.com") == ""

    def test_empty_discussions(self) -> None:
        assert detect_e2e_test_plan([], "https://example.com") == ""

    def test_non_dict_entries_skipped(self) -> None:
        bad_input: list[RawAPIDict] = ["not-a-dict"]  # type: ignore[list-item]
        assert detect_e2e_test_plan(bad_input, "https://example.com") == ""
