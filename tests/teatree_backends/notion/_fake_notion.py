"""An in-memory Notion API double — the ONLY thing these tests fake is HTTP.

Everything above the wire (the client, the block builder, the section locator,
the verify-after-write) runs for real against this store, so a section replace
is exercised end to end: blocks are created, ordered, archived and re-read the
way Notion actually does it. That is what makes the write-path tests worth
having — a mock of ``SectionWriter``'s own collaborators would only assert that
the code calls itself.
"""

import json
from typing import Any

import httpx


class FakeNotion:
    """A minimal Notion workspace: one page, its block tree, properties and comments.

    ``fail_with`` forces the next matching request to answer a chosen status +
    Notion error code, which is how the failure-path tests reach the classifier
    without inventing an exception to raise.

    Each ``suppress_*`` flag makes one mutation answer ``200`` while changing
    nothing — the shape Notion itself produces and the only way to prove a
    verify-after-write is load-bearing rather than decorative.
    """

    def __init__(self) -> None:
        self.page_id = "11111111-1111-1111-1111-111111111111"
        self.blocks: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[str]] = {self.page_id: []}
        self.comments: list[dict[str, Any]] = []
        self.properties: dict[str, dict[str, Any]] = {}
        self.archived: list[str] = []
        self.fail_with: tuple[int, str] | None = None
        self.identity_fail_with: tuple[int, str] | None = None
        self.requests: list[tuple[str, str]] = []
        self.bearer_tokens: list[str] = []
        self._counter = 0
        self.suppress_appends = False
        self.suppress_deletes = False
        self.suppress_comments = False
        self.suppress_property_writes = False
        self.page_archived = False
        self.page_parent: dict[str, Any] = {"type": "workspace", "workspace": True}
        # What `POST /v1/search` answers: the objects granted to this integration.
        self.shared_objects: list[dict[str, Any]] = [{"object": "page", "id": self.page_id}]
        self.rows: list[dict[str, Any]] | None = None
        self.query_fail_with: tuple[int, str] | None = None
        self.query_filters: list[dict[str, Any]] = []

    # ── store helpers ───────────────────────────────────────────────

    def add(self, block: dict[str, Any], *, parent: str = "", after: str = "") -> str:
        parent = parent or self.page_id
        self._counter += 1
        block_id = f"block-{self._counter:03d}"
        stored = {**block, "id": block_id, "object": "block", "has_children": False}
        _fill_plain_text(stored)
        nested = self._pop_children(stored)
        self.blocks[block_id] = stored
        siblings = self.children.setdefault(parent, [])
        position = siblings.index(after) + 1 if after in siblings else len(siblings)
        siblings.insert(position, block_id)
        for child in nested:
            self.add(child, parent=block_id)
        if nested:
            stored["has_children"] = True
        return block_id

    @staticmethod
    def _pop_children(stored: dict[str, Any]) -> list[dict[str, Any]]:
        payload = stored.get(stored.get("type", ""))
        if not isinstance(payload, dict):
            return []
        return payload.pop("children", [])

    def heading(self, text: str, *, toggle: bool = False, level: int = 2) -> str:
        return self.add(
            {
                "type": f"heading_{level}",
                f"heading_{level}": {"rich_text": [_span(text)], "is_toggleable": toggle},
            }
        )

    def paragraph(self, text: str, *, parent: str = "", after: str = "") -> str:
        return self.add({"type": "paragraph", "paragraph": {"rich_text": [_span(text)]}}, parent=parent, after=after)

    def set_property(self, name: str, payload: dict[str, Any]) -> None:
        self.properties[name] = {"id": f"prop-{len(self.properties) + 1}", **payload}

    def make_database_row(self, *, database_id: str, title_property: str = "Name", title: str = "backlog row") -> None:
        """Turn the page into a row of *database_id*, titled so the probe can look it up."""
        self.page_parent = {"type": "database_id", "database_id": database_id}
        self.set_property(title_property, {"type": "title", "title": [_span(title)]})
        self.rows = [{"id": self.page_id, "url": f"https://www.notion.so/{self.page_id}"}]

    def comment_texts(self) -> list[str]:
        return ["".join(span.get("plain_text", "") for span in item["rich_text"]) for item in self.comments]

    def text_of(self, block_id: str) -> str:
        block = self.blocks[block_id]
        payload = block.get(block.get("type", ""), {})
        return "".join(span.get("plain_text", "") for span in payload.get("rich_text", []))

    def body_texts(self, parent: str) -> list[str]:
        return [self.text_of(block_id) for block_id in self.children.get(parent, [])]

    # ── the wire ────────────────────────────────────────────────────

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        self.requests.append((request.method, path))
        self.bearer_tokens.append(request.headers.get("authorization", "").removeprefix("Bearer "))
        if path == "/users/me":
            return self._identity_response()
        if self.fail_with is not None:
            status, code = self.fail_with
            return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": code})
        return self._route(request, path)

    def _identity_response(self) -> httpx.Response:
        if self.identity_fail_with is not None:
            status, code = self.identity_fail_with
            return httpx.Response(status, json={"object": "error", "code": code, "message": code})
        return httpx.Response(
            200,
            json={"object": "user", "id": "bot-1", "name": "Factory", "bot": {"workspace_name": "Acme"}},
        )

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/search":
            return httpx.Response(200, json={"results": self.shared_objects, "has_more": False})
        if path.startswith("/pages/"):
            return self._page_response(request, path.removeprefix("/pages/"))
        if path == "/comments":
            if request.method == "POST":
                return self._create_comment_response(request)
            return httpx.Response(200, json={"results": self.comments, "has_more": False})
        if path.endswith("/query"):
            return self._query_response(request)
        return self._route_block(request, path)

    def _query_response(self, request: httpx.Request) -> httpx.Response:
        if self.query_fail_with is not None:
            status, code = self.query_fail_with
            return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": code})
        self.query_filters.append(json.loads(request.content.decode()).get("filter", {}))
        rows = self.rows if self.rows is not None else [{"id": "row-1"}]
        return httpx.Response(200, json={"results": rows, "has_more": False})

    def _page_response(self, request: httpx.Request, page_id: str) -> httpx.Response:
        if request.method == "PATCH" and not self.suppress_property_writes:
            payload = json.loads(request.content.decode())
            for name, value in payload.get("properties", {}).items():
                _fill_plain_text(value)
                self.properties[name] = {**self.properties.get(name, {}), **value}
        return httpx.Response(
            200,
            json={
                "object": "page",
                "id": page_id,
                "url": f"https://www.notion.so/{page_id}",
                "archived": self.page_archived,
                "in_trash": self.page_archived,
                "parent": self.page_parent,
                "properties": self.properties,
            },
        )

    def _create_comment_response(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        _fill_plain_text(payload)
        self._counter += 1
        created = {
            "object": "comment",
            "id": f"comment-{self._counter:03d}",
            "discussion_id": payload.get("discussion_id", f"disc-{self._counter:03d}"),
            "parent": payload.get("parent", {}),
            "created_by": {"object": "user", "id": "bot-1"},
            "created_time": "2026-07-29T00:00:00.000Z",
            "rich_text": payload["rich_text"],
        }
        if not self.suppress_comments:
            self.comments.append(created)
        return httpx.Response(200, json=created)

    def _route_block(self, request: httpx.Request, path: str) -> httpx.Response:
        if path.endswith("/children"):
            block_id = path.removeprefix("/blocks/").removesuffix("/children")
            if request.method == "GET":
                return self._children_response(block_id)
            return self._append_response(request, block_id)
        block_id = path.removeprefix("/blocks/")
        if request.method == "DELETE":
            return self._delete_response(block_id)
        return self._update_response(request, block_id)

    def _children_response(self, block_id: str) -> httpx.Response:
        results = [self.blocks[child] for child in self.children.get(block_id, [])]
        return httpx.Response(200, json={"results": results, "has_more": False})

    def _append_response(self, request: httpx.Request, block_id: str) -> httpx.Response:
        payload = json.loads(request.content.decode())
        if self.suppress_appends:
            return httpx.Response(200, json={"results": []})
        after = payload.get("after", "")
        created = []
        for child in payload["children"]:
            new_id = self.add(child, parent=block_id, after=after)
            after = new_id
            created.append(self.blocks[new_id])
        return httpx.Response(200, json={"results": created})

    def _delete_response(self, block_id: str) -> httpx.Response:
        self.archived.append(block_id)
        if self.suppress_deletes:
            # Notion answering 200 on a delete that leaves the block in place —
            # the shape the verify-after-write exists to catch.
            return httpx.Response(200, json={"object": "block", "id": block_id})
        for siblings in self.children.values():
            if block_id in siblings:
                siblings.remove(block_id)
        return httpx.Response(200, json={"object": "block", "id": block_id, "archived": True})

    def _update_response(self, request: httpx.Request, block_id: str) -> httpx.Response:
        payload = json.loads(request.content.decode())
        _fill_plain_text(payload)
        block = self.blocks[block_id]
        for key, value in payload.items():
            # Notion merges INTO the type payload — a PATCH naming only
            # ``rich_text`` leaves ``is_toggleable`` (and the rest) intact.
            existing = block.get(key)
            block[key] = {**existing, **value} if isinstance(existing, dict) and isinstance(value, dict) else value
        return httpx.Response(200, json=block)


def _span(text: str) -> dict[str, Any]:
    return {"type": "text", "plain_text": text, "annotations": {}, "text": {"content": text}}


def _fill_plain_text(value: Any) -> None:
    """Derive ``plain_text`` on every rich-text span, as the real API does.

    A client SENDS ``{"text": {"content": …}}`` and Notion READS BACK the same
    span carrying ``plain_text``. Without that the double would hand the
    verify-after-write an empty heading and every write would look like it never
    landed — a fake that is wrong in exactly the direction that hides bugs.
    """
    if isinstance(value, dict):
        content = value.get("text")
        if value.get("type") == "text" and isinstance(content, dict) and "plain_text" not in value:
            value["plain_text"] = content.get("content", "")
        for nested in value.values():
            _fill_plain_text(nested)
    elif isinstance(value, list):
        for item in value:
            _fill_plain_text(item)


def install_fake_notion(monkeypatch: Any) -> FakeNotion:
    """Point every ``httpx.Client`` in the process at a fresh :class:`FakeNotion`."""
    fake = FakeNotion()
    original = httpx.Client.__init__

    def patched(self: httpx.Client, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    return fake
