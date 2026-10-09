"""An in-memory Notion API double — the ONLY thing these tests fake is HTTP.

Everything above the wire (the client, the block builder, the section locator,
the verify-after-write) runs for real against this store, so a section replace
is exercised end to end: blocks are created, ordered, archived and re-read the
way Notion actually does it. That is what makes the write-path tests worth
having — a mock of ``SectionWriter``'s own collaborators would only assert that
the code calls itself.
"""

import json
import uuid
from typing import Any

import httpx

UNSEEN_OBJECT_FRAGMENTS = {
    "bot-name-and-id": "integration 'Factory' (bot id bot-1",
    "hedge-possible-causes": "Possible causes:",
    "cause-not-shared": "is not shared with this integration",
    "cause-id-gone": "no longer exists",
    "cause-other-workspace": "in another workspace",
    "check-connections": "Connections includes this integration",
    "check-bot-id-lookup": "`t3 notion whoami` shows the bot id",
    "check-id": "id is current",
    "check-parent": "parent is visible to this bot",
}


class FakeNotion:
    """A minimal Notion workspace: one page, its block tree, properties and comments.

    ``fail_with`` forces the next matching request to answer a chosen status +
    Notion error code, which is how the failure-path tests reach the classifier
    without inventing an exception to raise.

    Each ``suppress_*`` flag makes one mutation answer ``200`` while changing
    nothing — the shape Notion itself produces and the only way to prove a
    verify-after-write is load-bearing rather than decorative.

    A page PATCH naming ``archived`` flips the main page (``page_archived``) or a page
    ``POST /pages`` created; ``page_patches`` keeps every PATCH body as it arrived.
    """

    page_in_trash: bool | None = None

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
        self.suppress_archive = False
        self.page_archived = False
        self.page_patches: list[dict[str, Any]] = []
        self.page_parent: dict[str, Any] = {"type": "workspace", "workspace": True}
        # What `POST /v1/search` answers: the objects granted to this integration.
        self.shared_objects: list[dict[str, Any]] = [{"object": "page", "id": self.page_id}]
        self.unshared_pages: set[str] = set()
        self.unshared_blocks: set[str] = set()
        self.unshared_databases: set[str] = set()
        self.search_has_more = False
        self.rows: list[dict[str, Any]] | None = None
        # Per-object failure injection, so a walk can meet a permission gap on ONE
        # block while the rest of the tree stays readable.
        self.children_fail_for: dict[str, tuple[int, str]] = {}
        self.comments_fail_for: dict[str, tuple[int, str]] = {}
        # Comment ids served on the first read of their anchor and omitted after —
        # the shape a thread that vanishes between two enumerations produces.
        self.vanishing_comment_ids: set[str] = set()
        self._comment_reads: set[str] = set()
        self.children_page_size = 100
        self.query_fail_with: tuple[int, str] | None = None
        # What GET /databases/<id> reports under the data-source API version; an
        # empty list is Notion saying the integration was granted none of them.
        self.data_sources: list[dict[str, Any]] = [{"id": "data-source-1", "name": "rows"}]
        self.query_filters: list[dict[str, Any]] = []
        self.pages: dict[str, dict[str, Any]] = {}
        self.databases: set[str] = set()
        self.suppress_page_bodies = False
        self.suppress_page_titles = False
        self.trashed_blocks: set[str] = set()
        self.suppress_block_updates = False
        self.title_filter_misses = False
        self.query_fail_on_unfiltered: tuple[int, str] | None = None
        # A human edit that lands when the block is fetched on its own — i.e. after a page walk read it.
        self.edit_on_read: dict[str, str] = {}
        self.drop_formatting_on_update = False
        self.edit_cell_on_read: dict[str, tuple[int, str]] = {}
        self.fail_reads_after_update: tuple[int, str] | None = None
        self.update_then_fail: tuple[int, str] | None = None
        self.refuse_update: tuple[int, str] | None = None
        self.update_raises: tuple[type[httpx.TransportError], bool] | None = None
        self._updated = False
        self.rows_page_size = 100

    # ── store helpers ───────────────────────────────────────────────

    def add(self, block: dict[str, Any], *, parent: str = "", after: str = "", block_id: str = "") -> str:
        parent = parent or self.page_id
        self._counter += 1
        block_id = block_id or f"block-{self._counter:03d}"
        stored = {**block, "id": block_id, "object": "block", "has_children": False}
        _fill_plain_text(stored)
        nested = self._pop_children(stored)
        self.blocks[block_id] = stored
        siblings = self.children.setdefault(parent, [])
        position = siblings.index(after) + 1 if after in siblings else len(siblings)
        siblings.insert(position, block_id)
        if parent in self.blocks:
            self.blocks[parent]["has_children"] = True
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

    def comment_on(
        self,
        block_id: str,
        text: str,
        *,
        discussion_id: str = "",
        author: str = "Someone",
        created_time: str = "2026-07-01T00:00:00.000Z",
    ) -> str:
        """Anchor a comment on *block_id* — the inline shape a page-scoped read cannot see."""
        self._counter += 1
        comment_id = f"comment-{self._counter:03d}"
        self.comments.append(
            {
                "object": "comment",
                "id": comment_id,
                "parent": {"type": "block_id", "block_id": block_id},
                "discussion_id": discussion_id or f"disc-{self._counter:03d}",
                "created_time": created_time,
                "created_by": {"object": "user", "id": f"user-{self._counter:03d}"},
                "display_name": {"type": "user", "resolved_name": author},
                "rich_text": [_span(text)],
            }
        )
        return comment_id

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
            return _error(*self.identity_fail_with) if self.identity_fail_with else self._identity_response()
        # A forced failure targets the operation under test, never the parent read the write guard makes first.
        if self.fail_with is not None and not _is_parent_read(request, path):
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
            return httpx.Response(
                200,
                json={"results": self.shared_objects, "has_more": self.search_has_more, "next_cursor": "next"},
            )
        if path.endswith("/query"):
            return self._query_response(request)
        if path == "/pages" or path.startswith(("/pages/", "/databases/")):
            return self._page_or_database_response(request, path)
        if path == "/comments":
            return (
                self._create_comment_response(request)
                if request.method == "POST"
                else self._list_comments_response(request)
            )
        return self._route_block(request, path)

    def _page_or_database_response(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/pages":
            return self._create_page_response(request)
        if path.startswith("/pages/"):
            return self._page_response(request, path.removeprefix("/pages/"))
        database_id = path.removeprefix("/databases/")
        if database_id in self.unshared_databases:
            return httpx.Response(404, json={"object": "error", "status": 404, "code": "object_not_found"})
        title = {"Name": {"id": "title", "type": "title", "title": {}}}
        return httpx.Response(
            200,
            json={"object": "database", "id": database_id, "properties": title, "data_sources": self.data_sources},
        )

    def _create_page_response(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        _fill_plain_text(payload)
        properties = {name: {"id": "title", "type": "title", **value} for name, value in payload["properties"].items()}
        title = "".join(span["plain_text"] for prop in properties.values() for span in prop["title"])
        parent = payload["parent"]
        if "page_id" in parent:
            page_id = self.add(
                {"type": "child_page", "child_page": {"title": title}},
                parent=parent["page_id"],
                block_id=str(uuid.uuid4()),
            )
            link = {"type": "page_id", "page_id": parent["page_id"]}
        else:
            self._counter += 1
            page_id = f"row-{self._counter:03d}"
            link = {"type": "database_id", "database_id": parent["database_id"]}
        if self.suppress_page_titles:
            properties = {name: {**prop, "title": []} for name, prop in properties.items()}
        self.pages[page_id] = {"parent": link, "properties": properties, "icon": payload.get("icon")}
        self.children.setdefault(page_id, [])
        for child in [] if self.suppress_page_bodies else payload.get("children", []):
            self.add(child, parent=page_id)
        return httpx.Response(200, json=self._created_page(page_id))

    def _created_page(self, page_id: str) -> dict[str, Any]:
        page = self.pages[page_id]
        return {
            "object": "page",
            "id": page_id,
            "url": f"https://www.notion.so/{page_id}",
            "archived": page.get("archived", False),
            "in_trash": page.get("archived", False),
            "parent": page["parent"],
            "properties": page["properties"],
            "icon": page["icon"],
        }

    def _list_comments_response(self, request: httpx.Request) -> httpx.Response:
        """Answer per ANCHOR, as Notion does — a page-scoped read never sees a block-anchored thread."""
        block_id = request.url.params.get("block_id", "")
        failure = self.comments_fail_for.get(block_id)
        if failure is not None:
            status, code = failure
            return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": code})
        first_read = block_id not in self._comment_reads
        self._comment_reads.add(block_id)
        results = [
            item
            for item in self.comments
            if self._anchor_of(item) == block_id and (first_read or item.get("id") not in self.vanishing_comment_ids)
        ]
        return httpx.Response(200, json={"results": results, "has_more": False})

    def _anchor_of(self, comment: dict[str, Any]) -> str:
        parent = comment.get("parent")
        if isinstance(parent, dict):
            return str(parent.get("block_id") or parent.get("page_id") or self.page_id)
        return self.page_id

    def _query_response(self, request: httpx.Request) -> httpx.Response:
        if self.query_fail_with is not None:
            status, code = self.query_fail_with
            return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": code})
        payload = json.loads(request.content.decode())
        db_filter = payload.get("filter", {})
        if self.query_fail_on_unfiltered is not None and not db_filter:
            status, code = self.query_fail_on_unfiltered
            return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": code})
        self.query_filters.append(db_filter)
        rows = self.rows if self.rows is not None else [{"id": "row-1"}]
        if self.title_filter_misses and "title" in db_filter:
            rows = []
        start = int(payload.get("start_cursor") or 0)
        window = rows[start : start + self.rows_page_size]
        has_more = start + self.rows_page_size < len(rows)
        cursor = str(start + self.rows_page_size) if has_more else None
        return httpx.Response(200, json={"results": window, "has_more": has_more, "next_cursor": cursor})

    def _page_response(self, request: httpx.Request, page_id: str) -> httpx.Response:
        if page_id in self.unshared_pages:
            return httpx.Response(404, json={"object": "error", "status": 404, "code": "object_not_found"})
        if request.method == "PATCH":
            self._patch_page(page_id, json.loads(request.content.decode()))
        if page_id in self.pages:
            return httpx.Response(200, json=self._created_page(page_id))
        return httpx.Response(
            200,
            json={
                "object": "page",
                "id": page_id,
                "url": f"https://www.notion.so/{page_id}",
                "archived": self.page_archived,
                "in_trash": self.page_archived if self.page_in_trash is None else self.page_in_trash,
                "parent": self.page_parent,
                "properties": self.properties,
            },
        )

    def _patch_page(self, page_id: str, payload: dict[str, Any]) -> None:
        self.page_patches.append(payload)
        if "archived" in payload and not self.suppress_archive:
            if page_id in self.pages:
                self.pages[page_id]["archived"] = payload["archived"]
            else:
                self.page_archived = payload["archived"]
        if page_id in self.pages or self.suppress_property_writes:
            return
        for name, value in payload.get("properties", {}).items():
            _fill_plain_text(value)
            self.properties[name] = {**self.properties.get(name, {}), **value}

    def _create_comment_response(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        _fill_plain_text(payload)
        self._counter += 1
        created = {
            "object": "comment",
            "id": f"comment-{self._counter:03d}",
            "discussion_id": payload.get("discussion_id", f"disc-{self._counter:03d}"),
            "parent": payload.get("parent") or self._discussion_parent(payload.get("discussion_id", "")),
            "created_by": {"object": "user", "id": "bot-1"},
            "created_time": "2026-07-29T00:00:00.000Z",
            "rich_text": payload["rich_text"],
        }
        if not self.suppress_comments:
            self.comments.append(created)
        return httpx.Response(200, json=created)

    def _discussion_parent(self, discussion_id: str) -> dict[str, Any]:
        """A reply names only its discussion; Notion places it on that discussion's anchor."""
        return next((item["parent"] for item in self.comments if item.get("discussion_id") == discussion_id), {})

    def _route_block(self, request: httpx.Request, path: str) -> httpx.Response:
        if path.endswith("/children"):
            block_id = path.removeprefix("/blocks/").removesuffix("/children")
            if request.method != "GET":
                return self._append_response(request, block_id)
            if self._updated and self.fail_reads_after_update is not None:
                return _error(*self.fail_reads_after_update)
            return self._children_response(block_id, request.url.params.get("start_cursor", ""))
        block_id = path.removeprefix("/blocks/")
        if request.method == "GET":
            return self._block_response(block_id)
        if request.method == "DELETE":
            return self._delete_response(block_id)
        return self._update_response(request, block_id)

    def _block_response(self, block_id: str) -> httpx.Response:
        if block_id in self.unshared_blocks:
            return httpx.Response(404, json={"object": "error", "status": 404, "code": "object_not_found"})
        if block_id in self.edit_on_read:
            kind = self.blocks[block_id]["type"]
            self.blocks[block_id][kind]["rich_text"] = [_span(self.edit_on_read.pop(block_id))]
        if block_id in self.edit_cell_on_read:
            cell, text = self.edit_cell_on_read.pop(block_id)
            self.blocks[block_id]["table_row"]["cells"][cell] = [_span(text)]
        kind = "child_database" if block_id in self.databases else self.blocks.get(block_id, {}).get("type")
        return httpx.Response(
            200,
            json={
                **self.blocks.get(block_id, {}),
                "object": "block",
                "id": block_id,
                "type": kind or "child_page",
                "in_trash": block_id in self.trashed_blocks,
                "parent": self._parent_link(block_id),
            },
        )

    def _parent_link(self, block_id: str) -> dict[str, Any]:
        if block_id == self.page_id:
            return self.page_parent
        holder = next((parent for parent, kids in self.children.items() if block_id in kids), None)
        if holder is None:
            return {"type": "workspace", "workspace": True}
        if holder == self.page_id:
            return {"type": "page_id", "page_id": holder}
        return {"type": "block_id", "block_id": holder}

    def _children_response(self, block_id: str, cursor: str) -> httpx.Response:
        failure = self.children_fail_for.get(block_id)
        if failure is not None:
            status, code = failure
            return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": code})
        child_ids = self.children.get(block_id, [])
        start = child_ids.index(cursor) if cursor in child_ids else 0
        window = child_ids[start : start + self.children_page_size]
        has_more = start + self.children_page_size < len(child_ids)
        return httpx.Response(
            200,
            json={
                "results": [self.blocks[child] for child in window],
                "has_more": has_more,
                "next_cursor": child_ids[start + self.children_page_size] if has_more else None,
            },
        )

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

    def _update_failure(self, request: httpx.Request) -> httpx.TransportError:
        assert self.update_raises is not None
        reason = "update failed"
        return self.update_raises[0](reason, request=request)

    def _update_response(self, request: httpx.Request, block_id: str) -> httpx.Response:
        if self.refuse_update is not None:
            return _error(*self.refuse_update)
        if self.update_raises is not None and not self.update_raises[1]:
            raise self._update_failure(request)
        payload = json.loads(request.content.decode())
        _fill_plain_text(payload)
        block = self.blocks[block_id]
        if self.suppress_block_updates:
            return httpx.Response(200, json=block)
        if self.drop_formatting_on_update:
            for runs in _rich_text_lists(payload):
                for run in runs:
                    run["annotations"] = {}
        for key, value in payload.items():
            # Notion merges INTO the type payload — a PATCH naming only
            # ``rich_text`` leaves ``is_toggleable`` (and the rest) intact.
            existing = block.get(key)
            block[key] = {**existing, **value} if isinstance(existing, dict) and isinstance(value, dict) else value
        self._updated = True
        if self.update_raises is not None:
            raise self._update_failure(request)
        if self.update_then_fail is not None:
            return _error(*self.update_then_fail)
        return httpx.Response(200, json=block)


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": code})


def _rich_text_lists(payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
    found: list[list[dict[str, Any]]] = []
    for value in payload.values():
        if isinstance(value, dict):
            if isinstance(value.get("rich_text"), list):
                found.append(value["rich_text"])
            found.extend(cell for cell in value.get("cells", []) if isinstance(cell, list))
    return found


def _is_parent_read(request: httpx.Request, path: str) -> bool:
    return request.method == "GET" and path.startswith("/blocks/") and not path.endswith("/children")


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
    # The fake page is the internal root every write in these tests lands under.
    monkeypatch.setattr("teatree.backends.notion.write_guard.notion_write_roots", lambda _overlay: ([fake.page_id], []))
    return fake
