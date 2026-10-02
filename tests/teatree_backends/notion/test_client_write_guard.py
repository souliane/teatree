"""Every Notion mutation passes the write guard first, and a refused one never reaches Notion."""

import ast
import inspect
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from teatree.backends.notion import client as client_module
from teatree.backends.notion import write_guard
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionWriteRefusedError

_ROOT = "33333333-3333-3333-3333-333333333333"
_TARGET = "22222222-2222-2222-2222-222222222222"
_MUTATING_VERBS = frozenset({"patch", "delete", "put"})
# Notion answers these reads over POST; a new POST-issuing method goes through `_write` or joins this set on purpose.
_READ_POST_METHODS = frozenset({"_query", "any_object_shared", "search_shared_objects"})

_WRITES: dict[str, Callable[[NotionClient], Any]] = {
    "append_block_children": lambda notion: notion.append_block_children(_TARGET, [{"type": "divider", "divider": {}}]),
    "update_block": lambda notion: notion.update_block(_TARGET, {"divider": {}}),
    "delete_block": lambda notion: notion.delete_block(_TARGET),
    "update_page_properties": lambda notion: notion.update_page_properties(_TARGET, {"Status": {"status": {}}}),
    "create_comment": lambda notion: notion.create_comment(_TARGET, [{"type": "text", "text": {"content": "hi"}}]),
}


def _serve(monkeypatch: pytest.MonkeyPatch, methods: list[str]) -> None:
    parents = {
        _TARGET: {"type": "page_id", "page_id": _ROOT},
        _ROOT: {"type": "workspace", "workspace": True},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        block = request.url.path.removeprefix("/v1/blocks/")
        if request.method == "GET" and block in parents:
            return httpx.Response(200, json={"object": "block", "id": block, "parent": parents[block]})
        return httpx.Response(200, json={"object": "block", "id": _TARGET, "results": []})

    original = httpx.Client.__init__

    def patched(self: httpx.Client, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)


def _is_mutation(call: ast.Call, *, method: str) -> bool:
    attribute = call.func
    if not isinstance(attribute, ast.Attribute) or not isinstance(attribute.value, ast.Name):
        return False
    if attribute.value.id != "client":
        return False
    return attribute.attr in _MUTATING_VERBS or (attribute.attr == "post" and method not in _READ_POST_METHODS)


class TestAMutationPassesTheGuardFirst:
    @pytest.mark.parametrize("write", list(_WRITES.values()), ids=list(_WRITES))
    def test_a_refused_target_sends_no_mutating_request(
        self, monkeypatch: pytest.MonkeyPatch, write: Callable[[NotionClient], Any]
    ) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([], []))

        with pytest.raises(NotionWriteRefusedError):
            write(NotionClient(token="good"))

        assert set(methods) <= {"GET"}

    @pytest.mark.parametrize("write", list(_WRITES.values()), ids=list(_WRITES))
    def test_a_target_under_an_allowed_root_is_written(
        self, monkeypatch: pytest.MonkeyPatch, write: Callable[[NotionClient], Any]
    ) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([_ROOT], []))

        write(NotionClient(token="good"))

        assert set(methods) - {"GET"}

    def test_the_client_resolves_the_roots_of_its_own_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[str | None] = []
        _serve(monkeypatch, [])

        def roots(overlay: str | None) -> tuple[list[str], list[str]]:
            asked.append(overlay)
            return [_ROOT], []

        monkeypatch.setattr(write_guard, "notion_write_roots", roots)

        NotionClient(token="good", overlay="acme-overlay").delete_block(_TARGET)

        assert asked == ["acme-overlay"]


class TestNoMutationBypassesTheChokepoint:
    def test_every_mutating_request_is_sent_through_the_guarded_write(self) -> None:
        tree = ast.parse(inspect.getsource(client_module))
        client_class = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NotionClient"
        )
        unguarded = []
        for method in (node for node in client_class.body if isinstance(node, ast.FunctionDef)):
            calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]
            guarded = any(isinstance(call.func, ast.Attribute) and call.func.attr == "_write" for call in calls)
            if (
                method.name != "_write"
                and not guarded
                and any(_is_mutation(call, method=method.name) for call in calls)
            ):
                unguarded.append(method.name)

        assert not unguarded, f"Notion mutation(s) sent outside the write guard: {unguarded}"
