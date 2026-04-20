"""Tests for teatree.backends.slack — search_review_permalinks and helpers."""

from typing import Self

import httpx
import pytest

from teatree.backends.slack import (
    SlackReviewMatch,
    SlackReviewSearchRequest,
    _resolve_workspace_domain,
    search_review_permalinks,
)


class FakeClient:
    """Fake httpx.Client that returns configurable responses."""

    def __init__(
        self,
        *,
        auth_url: str = "https://myteam.slack.com/",
        pages: list[dict] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.auth_url = auth_url
        self.pages = pages or []
        self._page_idx = 0
        self.headers = headers or {}
        self.timeout = timeout

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        if "auth.test" in url:
            return httpx.Response(
                200,
                json={"ok": True, "url": self.auth_url},
                request=httpx.Request("GET", url),
            )
        if "conversations.history" in url:
            page = self.pages[self._page_idx] if self._page_idx < len(self.pages) else {"ok": False}
            self._page_idx += 1
            return httpx.Response(200, json=page, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))


def test_resolve_workspace_domain() -> None:
    """_resolve_workspace_domain extracts domain from auth.test response (lines 26-29)."""
    client = FakeClient(auth_url="https://myteam.slack.com/")
    domain = _resolve_workspace_domain(client)
    assert domain == "myteam.slack.com"


def test_resolve_workspace_domain_failed_auth() -> None:
    """_resolve_workspace_domain falls back when auth.test fails."""

    class FailingClient:
        def get(self, url: str) -> httpx.Response:
            return httpx.Response(500, request=httpx.Request("GET", url))

    domain = _resolve_workspace_domain(FailingClient())
    assert domain == "app.slack.com"


def test_search_review_permalinks_returns_empty_when_no_token() -> None:
    """search_review_permalinks returns [] when token is empty (line 47)."""
    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="",
            channel_id="C123",
            channel_name="review",
            mr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"],
        )
    )
    assert result == []


def test_search_review_permalinks_returns_empty_when_no_channel_id() -> None:
    """search_review_permalinks returns [] when channel_id is empty (line 47)."""
    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="",
            channel_name="review",
            mr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"],
        )
    )
    assert result == []


def test_search_review_permalinks_returns_empty_when_no_mr_urls() -> None:
    """search_review_permalinks returns [] when mr_urls is empty (line 47)."""
    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C123",
            channel_name="review",
            mr_urls=[],
        )
    )
    assert result == []


def test_search_review_permalinks_finds_matching_mr(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks matches MR URL in message text (lines 46-111)."""
    mr_url = "https://gitlab.com/org/repo/-/merge_requests/42"
    page = {
        "ok": True,
        "messages": [
            {
                "text": f"Please review {mr_url}",
                "ts": "1700000000.000100",
            },
        ],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://team.slack.com/", pages=[page])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C123",
            channel_name="review-crew",
            mr_urls=[mr_url],
        )
    )

    assert len(result) == 1
    assert result[0] == SlackReviewMatch(
        mr_url=mr_url,
        permalink="https://team.slack.com/archives/C123/p1700000000000100",
        channel="review-crew",
    )


def test_search_review_permalinks_stops_when_all_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks stops paging when all MR URLs are matched (line 102)."""
    mr_url = "https://gitlab.com/org/repo/-/merge_requests/10"
    page1 = {
        "ok": True,
        "messages": [{"text": f"Check {mr_url}", "ts": "1700000001.000200"}],
        "has_more": True,
        "response_metadata": {"next_cursor": "cursor2"},
    }
    page2 = {
        "ok": True,
        "messages": [{"text": "Unrelated message", "ts": "1700000002.000300"}],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://t.slack.com/", pages=[page1, page2])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C456",
            channel_name="reviews",
            mr_urls=[mr_url],
        )
    )

    assert len(result) == 1
    # Should not have fetched page2 since all URLs were found in page1
    assert fake_client._page_idx == 1


def test_search_review_permalinks_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks follows pagination cursor (lines 63-64, 106-109)."""
    mr_url1 = "https://gitlab.com/org/repo/-/merge_requests/1"
    mr_url2 = "https://gitlab.com/org/repo/-/merge_requests/2"
    page1 = {
        "ok": True,
        "messages": [{"text": f"Review {mr_url1}", "ts": "1700000001.000100"}],
        "has_more": True,
        "response_metadata": {"next_cursor": "page2cursor"},
    }
    page2 = {
        "ok": True,
        "messages": [{"text": f"Review {mr_url2}", "ts": "1700000002.000200"}],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://ws.slack.com/", pages=[page1, page2])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C789",
            channel_name="review",
            mr_urls=[mr_url1, mr_url2],
        )
    )

    assert len(result) == 2
    assert {m.mr_url for m in result} == {mr_url1, mr_url2}


def test_search_review_permalinks_stops_on_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks breaks when API returns ok=false (line 73-74)."""
    page = {"ok": False, "error": "channel_not_found"}
    fake_client = FakeClient(pages=[page])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C999",
            channel_name="review",
            mr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"],
        )
    )

    assert result == []


def test_search_review_permalinks_skips_messages_without_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks skips messages with empty ts (line 79-80)."""
    mr_url = "https://gitlab.com/org/repo/-/merge_requests/5"
    page = {
        "ok": True,
        "messages": [
            {"text": f"Review {mr_url}", "ts": ""},
            {"text": f"Review {mr_url}", "ts": "1700000003.000100"},
        ],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://ws.slack.com/", pages=[page])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C111",
            channel_name="review",
            mr_urls=[mr_url],
        )
    )

    assert len(result) == 1
    assert "p1700000003000100" in result[0].permalink


def test_search_review_permalinks_uses_provided_workspace_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks uses workspace_domain param if provided (line 57-58)."""
    mr_url = "https://gitlab.com/org/repo/-/merge_requests/7"
    page = {
        "ok": True,
        "messages": [{"text": f"Review {mr_url}", "ts": "1700000004.000100"}],
        "has_more": False,
    }
    fake_client = FakeClient(pages=[page])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C222",
            channel_name="review",
            mr_urls=[mr_url],
            workspace_domain="custom.slack.com",
        )
    )

    assert len(result) == 1
    assert "custom.slack.com" in result[0].permalink


def test_search_review_permalinks_deduplicates_same_mr(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks only returns first match for a given MR URL (line 92)."""
    mr_url = "https://gitlab.com/org/repo/-/merge_requests/8"
    page = {
        "ok": True,
        "messages": [
            {"text": f"Review {mr_url}", "ts": "1700000005.000100"},
            {"text": f"Also {mr_url}", "ts": "1700000006.000200"},
        ],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://ws.slack.com/", pages=[page])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C333",
            channel_name="review",
            mr_urls=[mr_url],
        )
    )

    assert len(result) == 1


def test_search_review_permalinks_stops_when_no_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks stops when has_more but no cursor (line 108-109)."""
    page = {
        "ok": True,
        "messages": [{"text": "No MR here", "ts": "1700000007.000100"}],
        "has_more": True,
        "response_metadata": {},
    }
    fake_client = FakeClient(pages=[page])
    monkeypatch.setattr("teatree.backends.slack.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C444",
            channel_name="review",
            mr_urls=["https://gitlab.com/org/repo/-/merge_requests/99"],
        )
    )

    assert result == []
    assert fake_client._page_idx == 1
