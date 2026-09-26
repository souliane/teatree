"""The paginated Notion reads: every page is collected, a stuck walk fails loud.

The ``has_more`` walks (search, database query, block children, comments) share one
paginator, so the contract is asserted once per surface: a cursor Notion hands
back a second time means the walk stopped advancing, and an unattended run must
raise rather than spin.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionError
from tests.teatree_backends.notion._fake_notion import install_fake_notion

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


def test_database_query_reaches_the_shared_notion_fake_query_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    notion = install_fake_notion(monkeypatch)
    notion.rows = [{"id": "row-1"}]

    rows = NotionClient(token="good").query_database("db-1", db_filter={"property": "Status"})

    assert [row["id"] for row in rows] == ["row-1"]
    assert notion.query_filters == [{"property": "Status"}]
    assert notion.requests == [("POST", "/databases/db-1/query")]


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

    def test_shared_object_search_pages_with_a_json_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = {
            "": {"results": [{"id": "page-a"}], "has_more": True, "next_cursor": "c1"},
            "c1": {"results": [{"id": "database-b"}], "has_more": False, "next_cursor": None},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            cursor = str(json.loads(request.content).get("start_cursor", ""))
            return httpx.Response(200, json=pages[cursor])

        _install(monkeypatch, handler)

        objects = NotionClient(token="good").search_shared_objects()

        assert [item["id"] for item in objects] == ["page-a", "database-b"]
