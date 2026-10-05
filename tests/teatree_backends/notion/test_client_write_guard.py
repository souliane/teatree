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
from tests.teatree_backends.notion._fake_notion import FakeNotion

_ROOT = "33333333-3333-3333-3333-333333333333"
_TARGET = "22222222-2222-2222-2222-222222222222"
_INNER = "55555555-5555-5555-5555-555555555555"
_MUTATING_VERBS = frozenset({"post", "patch", "put", "delete"})
_REQUEST_SENDERS = frozenset({"request", "send", "stream", "build_request"})
# Notion uses POST for these three reads; each known call shape is allowed once.
_READ_POST_TARGETS = {
    "NotionClient.any_object_shared": "f'{self._BASE}/search'",
    "NotionClient._query.request": "f'{self._BASE}/{path}'",
}

_WRITES: dict[str, Callable[[NotionClient], Any]] = {
    "append_block_children": lambda notion: notion.append_block_children(_TARGET, [{"type": "divider", "divider": {}}]),
    "update_block": lambda notion: notion.update_block(_TARGET, {"divider": {}}),
    "delete_block": lambda notion: notion.delete_block(_TARGET),
    "update_page": lambda notion: notion.update_page(_TARGET, {"properties": {"Status": {"status": {}}}}),
    "update_page_archived": lambda notion: notion.update_page(_TARGET, {"archived": True}),
}

_PUBLIC_WRITE_CASES: dict[str, tuple[Callable[[NotionClient], Any], bool]] = {
    "append_block_children": (lambda notion: notion.append_block_children(_TARGET, [{"in_trash": True}]), True),
    "update_block": (lambda notion: notion.update_block(_TARGET, {"in_trash": True}), True),
    "delete_block": (lambda notion: notion.delete_block(_TARGET), True),
    "update_page": (lambda notion: notion.update_page(_TARGET, {"in_trash": True}), True),
    "create_page": (
        lambda notion: notion.create_page(
            _TARGET,
            parent={"type": "page_id", "page_id": _TARGET},
            properties={},
            children=[{"in_trash": True}],
            icon="",
        ),
        True,
    ),
    "post_comment": (lambda notion: notion.post_comment(_TARGET, {"in_trash": True}, []), True),
}


def _serve(monkeypatch: pytest.MonkeyPatch, methods: list[str], *, inner_root: bool = False) -> None:
    parents = {
        _TARGET: {"type": "page_id", "page_id": _ROOT},
        _ROOT: {"type": "workspace", "workspace": True},
    }
    if inner_root:
        parents[_INNER] = {"type": "page_id", "page_id": _TARGET}

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


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"


def _is_get_request(call: ast.Call) -> bool:
    method = next((keyword.value for keyword in call.keywords if keyword.arg == "method"), None)
    if method is None and call.args:
        method = call.args[0]
    return isinstance(method, ast.Constant) and isinstance(method.value, str) and method.value.upper() == "GET"


def _is_mutating_transport_call(call: ast.Call, scope: str, read_posts: set[str]) -> bool:
    name = call.func.attr if isinstance(call.func, ast.Attribute) else None
    if (
        name == "post"
        and scope in _READ_POST_TARGETS
        and call.args
        and ast.unparse(call.args[0]) == _READ_POST_TARGETS[scope]
        and scope not in read_posts
    ):
        read_posts.add(scope)
        return False
    if name in _MUTATING_VERBS:
        return True
    if name in _REQUEST_SENDERS:
        return not _is_get_request(call)
    return any(keyword.arg == "method" and not _is_get_request(call) for keyword in call.keywords)


def _scan_client_source(source: str) -> tuple[set[str], list[str]]:
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    client_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NotionClient")
    methods = {
        node.name: node for node in client_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    read_posts: set[str] = set()
    mutations = [
        (call, _scope(call, parents))
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _is_mutating_transport_call(call, _scope(call, parents), read_posts)
    ]
    unguarded = [f"{scope}:{call.lineno}" for call, scope in mutations if scope != "NotionClient._write"]

    mutating_methods = {
        scope.split(".")[1]
        for _, scope in mutations
        if scope.startswith("NotionClient.") and scope.split(".")[1] in methods
    }
    calls_by_method = {
        name: {
            call.func.attr
            for call in ast.walk(method)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        }
        for name, method in methods.items()
    }
    write_paths = {"_write", *mutating_methods}
    while callers := {name for name, calls in calls_by_method.items() if calls & write_paths} - write_paths:
        write_paths.update(callers)
    return {name for name in write_paths if not name.startswith("_")}, unguarded


class TestAMutationPassesTheGuardFirst:
    @pytest.mark.parametrize("endpoint", ["block", "page"])
    @pytest.mark.parametrize("relationship", ["root", "ancestor", "unrelated"])
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"is_archived": True}, id="is_archived-true"),
            pytest.param({"isArchived": True}, id="isArchived-true"),
            pytest.param({"in_trash": "true"}, id="in_trash-string"),
            pytest.param({"archived": 1}, id="archived-integer"),
            pytest.param({"children": [{"in-trash": None}]}, id="nested-in-trash-null"),
            pytest.param({"ARCHIVED": []}, id="archived-list"),
            pytest.param({"inTrash": {}}, id="inTrash-dict"),
            pytest.param({"archived": False, "is_archived": True}, id="mixed-restore-and-archive"),
        ],
    )
    def test_non_false_archive_payload_uses_archive_guard(
        self, monkeypatch: pytest.MonkeyPatch, endpoint: str, relationship: str, payload: dict[str, Any]
    ) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods, inner_root=relationship == "ancestor")
        roots = {
            "root": [_ROOT, _TARGET],
            "ancestor": [_ROOT, _INNER],
            "unrelated": [_ROOT],
        }
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: (roots[relationship], []))
        client = NotionClient(token="good")
        update = client.update_block if endpoint == "block" else client.update_page

        if relationship == "unrelated":
            update(_TARGET, payload)
            assert "PATCH" in methods
        else:
            reason = "it is a configured write root" if relationship == "root" else "contains the configured write root"
            with pytest.raises(NotionWriteRefusedError, match=reason):
                update(_TARGET, payload)
            assert set(methods) <= {"GET"}

    @pytest.mark.parametrize("endpoint", ["block", "page"])
    @pytest.mark.parametrize("relationship", ["root", "ancestor"])
    @pytest.mark.parametrize(
        "payload",
        [
            {"archived": False},
            {"in_trash": False},
            {"is_archived": False},
            {"isArchived": False},
            {"inTrash": False},
            {"in-trash": False},
            {"properties": {"x": {"in_trash": False}}},
        ],
    )
    def test_false_archive_payload_remains_allowed(
        self, monkeypatch: pytest.MonkeyPatch, endpoint: str, relationship: str, payload: dict[str, Any]
    ) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods, inner_root=relationship == "ancestor")
        roots = [_ROOT, _TARGET] if relationship == "root" else [_ROOT, _INNER]
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: (roots, []))
        client = NotionClient(token="good")
        update = client.update_block if endpoint == "block" else client.update_page

        update(_TARGET, payload)

        assert "PATCH" in methods

    @pytest.mark.parametrize(
        "properties",
        [
            {"Archived": {"checkbox": True}},
            {"x": {"in_trash": True}},
        ],
    )
    def test_page_properties_are_user_data(self, monkeypatch: pytest.MonkeyPatch, properties: dict[str, Any]) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([_ROOT, _TARGET], []))

        NotionClient(token="good").update_page(_TARGET, {"properties": properties})

        assert "PATCH" in methods

    @pytest.mark.parametrize("endpoint", ["block", "page"])
    @pytest.mark.parametrize("flag", ["archived", "in_trash"])
    @pytest.mark.parametrize("relationship", ["root", "ancestor", "unrelated"])
    def test_trash_payload_uses_archive_guard(
        self, monkeypatch: pytest.MonkeyPatch, endpoint: str, flag: str, relationship: str
    ) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods, inner_root=relationship == "ancestor")
        roots = {
            "root": [_ROOT, _TARGET],
            "ancestor": [_ROOT, _INNER],
            "unrelated": [_ROOT],
        }
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: (roots[relationship], []))
        payload = {"divider": {}, flag: True} if endpoint == "block" else {"properties": {}, flag: True}
        client = NotionClient(token="good")
        update = client.update_block if endpoint == "block" else client.update_page

        if relationship == "unrelated":
            update(_TARGET, payload)
            assert "PATCH" in methods
        else:
            reason = "it is a configured write root" if relationship == "root" else "contains the configured write root"
            with pytest.raises(NotionWriteRefusedError, match=reason):
                update(_TARGET, payload)
            assert set(methods) <= {"GET"}

    @pytest.mark.parametrize("endpoint", ["block", "page"])
    @pytest.mark.parametrize("flag", ["archived", "in_trash"])
    def test_restore_payload_remains_allowed_on_a_root(
        self, monkeypatch: pytest.MonkeyPatch, endpoint: str, flag: str
    ) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([_ROOT, _TARGET], []))
        payload = {flag: False}

        if endpoint == "block":
            NotionClient(token="good").update_block(_TARGET, payload)
        else:
            NotionClient(token="good").update_page(_TARGET, payload)

        assert "PATCH" in methods

    def test_block_delete_cannot_trash_a_configured_root(
        self, monkeypatch: pytest.MonkeyPatch, notion: FakeNotion
    ) -> None:
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([notion.page_id], []))

        with pytest.raises(NotionWriteRefusedError, match=r"refusing to archive .*it is a configured write root"):
            NotionClient(token="good").delete_block(notion.page_id)

        assert notion.archived == []

    def test_block_delete_cannot_trash_an_ancestor_of_a_configured_root(
        self, monkeypatch: pytest.MonkeyPatch, notion: FakeNotion
    ) -> None:
        inner_root = "55555555-5555-5555-5555-555555555555"
        notion.page_parent = {"type": "page_id", "page_id": _ROOT}
        notion.add({"type": "child_page", "child_page": {"title": "Protected"}}, block_id=inner_root)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([_ROOT, inner_root], []))

        with pytest.raises(NotionWriteRefusedError, match=r"refusing to archive .*contains the configured write root"):
            NotionClient(token="good").delete_block(notion.page_id)

        assert notion.archived == []

    def test_block_delete_refuses_when_a_configured_roots_parent_chain_cannot_be_read(
        self, monkeypatch: pytest.MonkeyPatch, notion: FakeNotion
    ) -> None:
        inner_root = "55555555-5555-5555-5555-555555555555"
        notion.page_parent = {"type": "page_id", "page_id": _ROOT}
        notion.add({"type": "child_page", "child_page": {"title": "Protected"}}, block_id=inner_root)
        notion.unshared_blocks.add(inner_root)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([_ROOT, inner_root], []))

        with pytest.raises(NotionWriteRefusedError, match=r"parent of .* could not be read"):
            NotionClient(token="good").delete_block(notion.page_id)

        assert notion.archived == []

    def test_block_delete_trashes_an_ordinary_child_under_an_allowed_root(
        self, monkeypatch: pytest.MonkeyPatch, notion: FakeNotion
    ) -> None:
        block_id = notion.paragraph("ordinary")
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([notion.page_id], []))

        deleted = NotionClient(token="good").delete_block(block_id)

        assert deleted["archived"] is True
        assert notion.archived == [block_id]

    def test_page_patch_cannot_archive_a_configured_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([_ROOT, _TARGET], []))

        with pytest.raises(NotionWriteRefusedError, match="it is a configured write root"):
            NotionClient(token="good").update_page(_TARGET, {"archived": True})

        assert set(methods) <= {"GET"}

    def test_page_patch_cannot_archive_an_ancestor_of_a_configured_root(
        self, monkeypatch: pytest.MonkeyPatch, notion: FakeNotion
    ) -> None:
        inner_root = "55555555-5555-5555-5555-555555555555"
        notion.page_parent = {"type": "page_id", "page_id": _ROOT}
        notion.add({"type": "child_page", "child_page": {"title": "Protected"}}, block_id=inner_root)
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: ([_ROOT, inner_root], []))

        with pytest.raises(NotionWriteRefusedError, match="contains the configured write root"):
            NotionClient(token="good").update_page(notion.page_id, {"archived": True})

        assert notion.page_patches == []

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
    def test_scanner_finds_a_direct_post_after_an_unrelated_call(self) -> None:
        source = """\
class NotionClient:
    def _write(self):
        pass

    def bypass(self):
        self.audit()
        return self._http.post("/pages", json={})
"""

        public_writes, unguarded = _scan_client_source(source)

        assert public_writes == {"bypass"}
        assert unguarded == ["NotionClient.bypass:7"]

    @pytest.mark.parametrize("method_name", _PUBLIC_WRITE_CASES)
    @pytest.mark.parametrize("protected", [True, False], ids=["protected-root", "unrelated-page"])
    def test_every_public_write_handles_a_trash_payload_at_the_send_seam(
        self, monkeypatch: pytest.MonkeyPatch, method_name: str, *, protected: bool
    ) -> None:
        methods: list[str] = []
        _serve(monkeypatch, methods)
        roots = [_ROOT, _TARGET] if protected else [_ROOT]
        monkeypatch.setattr(write_guard, "notion_write_roots", lambda _overlay: (roots, []))
        write, carries_trash = _PUBLIC_WRITE_CASES[method_name]

        if protected and carries_trash:
            with pytest.raises(NotionWriteRefusedError, match="configured write root"):
                write(NotionClient(token="good"))
            assert set(methods) <= {"GET"}
        else:
            write(NotionClient(token="good"))
            assert set(methods) - {"GET"}

    def test_every_mutating_request_is_sent_through_the_guarded_write(self) -> None:
        public_writes, unguarded = _scan_client_source(inspect.getsource(client_module))

        assert public_writes == _PUBLIC_WRITE_CASES.keys(), (
            f"Notion public writes missing cases: {sorted(public_writes - _PUBLIC_WRITE_CASES.keys())}; "
            f"stale cases: {sorted(_PUBLIC_WRITE_CASES.keys() - public_writes)}"
        )
        assert not unguarded, f"Notion mutation(s) sent outside the write guard: {unguarded}"
