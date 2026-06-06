"""Tests for teatree.backends.slack — search_review_permalinks and helpers."""

from typing import Self

import httpx
import pytest

from teatree.backends.slack import (
    SlackReviewMatch,
    SlackReviewSearchRequest,
    read_recent_review_matches,
    search_review_permalinks,
)
from teatree.backends.slack.client import _resolve_workspace_domain


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
            pr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"],
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
            pr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"],
        )
    )
    assert result == []


def test_search_review_permalinks_returns_empty_when_no_pr_urls() -> None:
    """search_review_permalinks returns [] when pr_urls is empty (line 47)."""
    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C123",
            channel_name="review",
            pr_urls=[],
        )
    )
    assert result == []


def test_search_review_permalinks_finds_matching_mr(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks matches PR URL in message text (lines 46-111)."""
    pr_url = "https://gitlab.com/org/repo/-/merge_requests/42"
    page = {
        "ok": True,
        "messages": [
            {
                "text": f"Please review {pr_url}",
                "ts": "1700000000.000100",
            },
        ],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://team.slack.com/", pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C123",
            channel_name="review-team",
            pr_urls=[pr_url],
        )
    )

    assert len(result) == 1
    assert result[0] == SlackReviewMatch(
        pr_url=pr_url,
        permalink="https://team.slack.com/archives/C123/p1700000000000100",
        channel="review-team",
        ts="1700000000.000100",
        author="",
    )


def test_search_review_permalinks_stops_when_all_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks stops paging when all PR URLs are matched (line 102)."""
    pr_url = "https://gitlab.com/org/repo/-/merge_requests/10"
    page1 = {
        "ok": True,
        "messages": [{"text": f"Check {pr_url}", "ts": "1700000001.000200"}],
        "has_more": True,
        "response_metadata": {"next_cursor": "cursor2"},
    }
    page2 = {
        "ok": True,
        "messages": [{"text": "Unrelated message", "ts": "1700000002.000300"}],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://t.slack.com/", pages=[page1, page2])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C456",
            channel_name="reviews",
            pr_urls=[pr_url],
        )
    )

    assert len(result) == 1
    # Should not have fetched page2 since all URLs were found in page1
    assert fake_client._page_idx == 1


def test_search_review_permalinks_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks follows pagination cursor (lines 63-64, 106-109)."""
    pr_url1 = "https://gitlab.com/org/repo/-/merge_requests/1"
    pr_url2 = "https://gitlab.com/org/repo/-/merge_requests/2"
    page1 = {
        "ok": True,
        "messages": [{"text": f"Review {pr_url1}", "ts": "1700000001.000100"}],
        "has_more": True,
        "response_metadata": {"next_cursor": "page2cursor"},
    }
    page2 = {
        "ok": True,
        "messages": [{"text": f"Review {pr_url2}", "ts": "1700000002.000200"}],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://ws.slack.com/", pages=[page1, page2])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C789",
            channel_name="review",
            pr_urls=[pr_url1, pr_url2],
        )
    )

    assert len(result) == 2
    assert {m.pr_url for m in result} == {pr_url1, pr_url2}


def test_search_review_permalinks_stops_on_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks breaks when API returns ok=false (line 73-74)."""
    page = {"ok": False, "error": "channel_not_found"}
    fake_client = FakeClient(pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C999",
            channel_name="review",
            pr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"],
        )
    )

    assert result == []


def test_search_review_permalinks_skips_messages_without_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks skips messages with empty ts (line 79-80)."""
    pr_url = "https://gitlab.com/org/repo/-/merge_requests/5"
    page = {
        "ok": True,
        "messages": [
            {"text": f"Review {pr_url}", "ts": ""},
            {"text": f"Review {pr_url}", "ts": "1700000003.000100"},
        ],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://ws.slack.com/", pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C111",
            channel_name="review",
            pr_urls=[pr_url],
        )
    )

    assert len(result) == 1
    assert "p1700000003000100" in result[0].permalink


def test_search_review_permalinks_uses_provided_workspace_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks uses workspace_domain param if provided (line 57-58)."""
    pr_url = "https://gitlab.com/org/repo/-/merge_requests/7"
    page = {
        "ok": True,
        "messages": [{"text": f"Review {pr_url}", "ts": "1700000004.000100"}],
        "has_more": False,
    }
    fake_client = FakeClient(pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C222",
            channel_name="review",
            pr_urls=[pr_url],
            workspace_domain="custom.slack.com",
        )
    )

    assert len(result) == 1
    assert "custom.slack.com" in result[0].permalink


def test_search_review_permalinks_deduplicates_same_mr(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks only returns first match for a given PR URL (line 92)."""
    pr_url = "https://gitlab.com/org/repo/-/merge_requests/8"
    page = {
        "ok": True,
        "messages": [
            {"text": f"Review {pr_url}", "ts": "1700000005.000100"},
            {"text": f"Also {pr_url}", "ts": "1700000006.000200"},
        ],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://ws.slack.com/", pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C333",
            channel_name="review",
            pr_urls=[pr_url],
        )
    )

    assert len(result) == 1


def test_search_review_permalinks_stops_when_no_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_review_permalinks stops when has_more but no cursor (line 108-109)."""
    page = {
        "ok": True,
        "messages": [{"text": "No PR here", "ts": "1700000007.000100"}],
        "has_more": True,
        "response_metadata": {},
    }
    fake_client = FakeClient(pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    result = search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C444",
            channel_name="review",
            pr_urls=["https://gitlab.com/org/repo/-/merge_requests/99"],
        )
    )

    assert result == []


class _ParamCapturingClient(FakeClient):
    """FakeClient that records conversations.history query params."""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)
        self.history_params: list[dict] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        if "conversations.history" in url:
            params = kwargs.get("params")
            if isinstance(params, dict):
                self.history_params.append(params)
        return super().get(url, **kwargs)


def test_oldest_ts_is_passed_as_oldest_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """SlackReviewSearchRequest.oldest_ts bounds conversations.history (#1084)."""
    page = {"ok": True, "messages": [], "has_more": False}
    fake_client = _ParamCapturingClient(pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    search_review_permalinks(
        SlackReviewSearchRequest(
            token="xoxb-token",
            channel_id="C1",
            channel_name="review",
            pr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"],
            oldest_ts="1700000000.000000",
        )
    )

    assert fake_client.history_params
    assert fake_client.history_params[0]["oldest"] == "1700000000.000000"


def test_read_recent_review_matches_returns_empty_ok_when_no_token() -> None:
    """A short-circuit (no token/channel/urls) is a clean ok=True empty read."""
    read = read_recent_review_matches(
        SlackReviewSearchRequest(token="", channel_id="C1", channel_name="r", pr_urls=["x"])
    )
    assert read.ok is True
    assert read.matches == []


def test_read_recent_review_matches_clean_empty_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean read that finds nothing is ok=True, matches=[] (safe to post)."""
    fake_client = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    read = read_recent_review_matches(
        SlackReviewSearchRequest(token="xoxb", channel_id="C1", channel_name="r", pr_urls=["https://x/pull/1"])
    )
    assert read.ok is True
    assert read.matches == []


def test_read_recent_review_matches_not_ok_when_api_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API ok=false page means ok=False (fail safe to NOT posting)."""
    fake_client = FakeClient(pages=[{"ok": False, "error": "channel_not_found"}])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    read = read_recent_review_matches(
        SlackReviewSearchRequest(token="xoxb", channel_id="C1", channel_name="r", pr_urls=["https://x/pull/1"])
    )
    assert read.ok is False
    assert read.matches == []


def test_read_recent_review_matches_finds_and_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches are returned across pages with ts/author populated."""
    pr_url = "https://gitlab.com/org/repo/-/merge_requests/7"
    page1 = {
        "ok": True,
        "messages": [{"text": "nothing here", "ts": "1700000000.000100"}],
        "has_more": True,
        "response_metadata": {"next_cursor": "c2"},
    }
    page2 = {
        "ok": True,
        "messages": [{"text": f"review {pr_url}", "ts": "1700000001.000200", "user": "U9"}],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://w.slack.com/", pages=[page1, page2])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    read = read_recent_review_matches(
        SlackReviewSearchRequest(token="xoxb", channel_id="C1", channel_name="r", pr_urls=[pr_url])
    )
    assert read.ok is True
    assert len(read.matches) == 1
    assert read.matches[0].ts == "1700000001.000200"
    assert read.matches[0].author == "U9"


def test_read_recent_review_matches_stops_when_no_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """has_more but no cursor terminates the loop with a clean ok read."""
    fake_client = FakeClient(pages=[{"ok": True, "messages": [], "has_more": True, "response_metadata": {}}])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    read = read_recent_review_matches(
        SlackReviewSearchRequest(token="xoxb", channel_id="C1", channel_name="r", pr_urls=["https://x/pull/1"])
    )
    assert read.ok is True


def test_iter_review_matches_uses_bot_id_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bot-authored message records bot_id as the match author."""
    pr_url = "https://gitlab.com/org/repo/-/merge_requests/3"
    page = {
        "ok": True,
        "messages": [{"text": f"review {pr_url}", "ts": "1700000002.000100", "bot_id": "B42"}],
        "has_more": False,
    }
    fake_client = FakeClient(auth_url="https://w.slack.com/", pages=[page])
    monkeypatch.setattr("teatree.backends.slack.client.httpx.Client", lambda **kw: fake_client)

    read = read_recent_review_matches(
        SlackReviewSearchRequest(token="xoxb", channel_id="C1", channel_name="r", pr_urls=[pr_url])
    )
    assert read.matches[0].author == "B42"
    assert fake_client._page_idx == 1
