"""The paginated Notion reads: every page is collected, a stuck walk fails loud.

The three ``has_more`` walks (database query, block children, comments) share one
paginator, so the contract is asserted once per surface: a cursor Notion hands
back a second time means the walk stopped advancing, and an unattended run must
raise rather than spin.
"""

from collections.abc import Callable

import httpx
import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionError

_RUNAWAY_PAGES = 6


def _install(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    original = httpx.Client.__init__

    def patched(self: httpx.Client, **kwargs: object) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)


def _stuck_handler(calls: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer every page with the SAME cursor, aborting a walk that will not stop."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) > _RUNAWAY_PAGES:
            never = "the walk kept paging on a repeated cursor instead of failing loud"
            raise AssertionError(never)
        return httpx.Response(200, json={"results": [{"id": "row"}], "has_more": True, "next_cursor": "stuck"})

    return handler


def _paging_handler(pages: dict[str, dict[str, object]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("start_cursor", "")
        return httpx.Response(200, json=pages[str(cursor)])

    return handler


class TestRepeatedCursor:
    def test_a_database_query_refuses_a_cursor_notion_repeats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        _install(monkeypatch, _stuck_handler(calls))

        with pytest.raises(NotionError, match="not advancing"):
            NotionClient(token="good").query_database("db-1")

        assert len(calls) == 2

    def test_a_comment_read_refuses_a_cursor_notion_repeats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        _install(monkeypatch, _stuck_handler(calls))

        with pytest.raises(NotionError, match="not advancing"):
            NotionClient(token="good").list_comments("block-1")

        assert len(calls) == 2

    def test_a_block_children_read_refuses_a_cursor_notion_repeats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        _install(monkeypatch, _stuck_handler(calls))

        with pytest.raises(NotionError, match="not advancing"):
            NotionClient(token="good").list_block_children("block-1")

        assert len(calls) == 2


class TestAdvancingCursor:
    def test_distinct_cursors_are_followed_to_the_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _paging_handler(
                {
                    "": {"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"},
                    "c1": {"results": [{"id": "b"}], "has_more": True, "next_cursor": "c2"},
                    "c2": {"results": [{"id": "c"}], "has_more": False, "next_cursor": None},
                }
            ),
        )

        children = NotionClient(token="good").list_block_children("block-1")

        assert [child["id"] for child in children] == ["a", "b", "c"]
