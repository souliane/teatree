"""Headless Notion access over the public API with an integration token.

The claude.ai Notion connector is interactively authenticated and simply absent
from a cron/headless run, so a factory driving it can read no PRD and write no
section once nobody is watching. This client is the headless replacement: a
Notion **internal integration** token, resolved from the ``pass`` store through
the same :class:`~teatree.llm.credentials.Credential` machinery every other
teatree service token uses, against the documented public API.

The setup an integration token implies, and that no code can do for the
operator: the integration must be **explicitly shared onto each page and
database** it touches. Until that grant exists Notion answers 404, which
:class:`~teatree.backends.notion.errors.NotionErrorClassifier` reports as
:class:`~teatree.backends.notion.errors.NotionNotSharedError` rather than as a
missing page; the same 404 also answers a deleted id and another workspace's page,
so that error names the bot and the checks that separate the three.

Reads run under the shared bounded-retry transport
(:class:`~teatree.backends.http_retry.SimpleRetryTransport`, knobs from
``T3_NOTION_HTTP_*``); every mutation is non-idempotent and is retried only on a
CONNECT-phase failure, never replayed once the request reached Notion.
"""

from collections.abc import Callable
from typing import cast

import httpx

from teatree.backends.http_retry import SimpleRetryTransport
from teatree.backends.notion.errors import NotionBadTokenError, NotionError, NotionErrorClassifier
from teatree.backends.notion.liveness import LivenessVerdict, PageLivenessProbe
from teatree.backends.notion.write_guard import WriteGuard, WriteScope
from teatree.core.overlays.notion_identity import NOTION_CREDENTIAL_ENV_VAR
from teatree.llm.credentials import Credential, CredentialSpec
from teatree.types import RawAPIDict

#: Notion refuses an append carrying more than this many blocks in one request.
APPEND_BATCH_SIZE = 100

#: The pinned API version. ``2022-06-28`` is the long-stable contract the page,
#: block, comment and database endpoints below are written against.
DEFAULT_API_VERSION = "2022-06-28"

#: ``/v1/data_sources/{id}/query`` exists only from this version onward; the
#: request that needs it carries this header instead of the pinned default.
DATA_SOURCE_API_VERSION = "2025-09-03"

_NORMALIZED_ARCHIVE_FLAG_KEYS = frozenset({"archived", "intrash", "isarchived"})

type PagedRequest = Callable[[httpx.Client, str | None], httpx.Response]


class NotionTokenCredential(Credential):
    """The Notion internal-integration token — env first, then the venue's routed ``pass`` entry.

    Routes through the provider-neutral :class:`~teatree.llm.credentials.Credential`
    machinery (identical to ``FigmaTokenCredential``) so a rotated ``NOTION_TOKEN``
    always beats a stale ``pass`` entry, the value never reaches argv, and an
    absent credential fails loud naming the fix instead of authenticating as
    nothing. There is no default entry: the caller injects ``pass_path_override``
    from the ``notion_token_pass_key`` setting.
    """

    spec = CredentialSpec(
        env_var=NOTION_CREDENTIAL_ENV_VAR, conflicting_vars=(), routing_setting="notion_token_pass_key"
    )


def option_name(prop: object) -> str | None:
    """Read the option name from a Notion ``status``- or ``select``-typed property."""
    if not isinstance(prop, dict):
        return None
    typed = cast("RawAPIDict", prop)
    for key in ("status", "select"):
        value = typed.get(key)
        if isinstance(value, dict):
            name = cast("RawAPIDict", value).get("name")
            if isinstance(name, str):
                return name
    return None


def _archives_or_trashes(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    children = payload.get("children")
    blocks = children if isinstance(children, list) else []
    return any(
        isinstance(key, str)
        and key.casefold().replace("_", "").replace("-", "") in _NORMALIZED_ARCHIVE_FLAG_KEYS
        and value is not False
        for body in (payload, *blocks)
        if isinstance(body, dict)
        for key, value in body.items()
    )


class NotionClient:
    """Notion API client — pages, blocks, comments, databases, status writes."""

    _BASE = "https://api.notion.com/v1"

    def __init__(self, *, token: str, version: str = DEFAULT_API_VERSION, overlay: str | None = None) -> None:
        self.token = token
        self.version = version
        self.overlay = overlay
        self._transport = SimpleRetryTransport(env_prefix="T3_NOTION_HTTP")
        self._errors = NotionErrorClassifier(self.describe_identity)
        self._write_guard = WriteGuard(parent_of=self._parent_of, scope=lambda: WriteScope.for_overlay(overlay))

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
            },
            timeout=10.0,
        )

    # ── identity ────────────────────────────────────────────────────────

    def whoami(self) -> RawAPIDict:
        """Return the bot user this token authenticates as (``GET /users/me``)."""
        with self._client() as client:
            response = self._transport.run(lambda: client.get(f"{self._BASE}/users/me"), idempotent=True)
            self._raise_identity_error(response)
            return cast("RawAPIDict", response.json())

    def describe_identity(self) -> str:
        """A one-line human description of the integration, for a 404 diagnostic."""
        body = self.whoami()
        name = str(body.get("name") or "unnamed integration")
        workspace = self._workspace_name(body)
        suffix = f", workspace {workspace!r}" if workspace else ""
        return f"integration {name!r} (bot id {body.get('id', '?')}{suffix})"

    @staticmethod
    def _workspace_name(body: RawAPIDict) -> str:
        bot = body.get("bot")
        if not isinstance(bot, dict):
            return ""
        return str(cast("RawAPIDict", bot).get("workspace_name") or "")

    @staticmethod
    def _raise_identity_error(response: httpx.Response) -> None:
        """Classify a ``/users/me`` failure WITHOUT re-entering the identity probe.

        The classifier's 404 branch calls back into the identity probe, so the
        probe itself must never route through it. Only 401 is meaningful here —
        anything else is an ordinary HTTP failure the caller re-raises.
        """
        if not response.is_error:
            return
        if response.status_code == httpx.codes.UNAUTHORIZED:
            msg = (
                "Notion rejected the integration token (HTTP 401). It is invalid, revoked, "
                "or belongs to a deleted integration — issue a new internal integration "
                "secret and store it again."
            )
            raise NotionBadTokenError(msg)
        response.raise_for_status()

    def any_object_shared(self) -> bool:
        """Whether ANY page or database has been shared with this integration.

        ``POST /v1/search`` returns only granted objects, so an empty result is the
        precise "the token authenticates but was never shared onto anything" state —
        which every later read reports indistinguishably as a 404 on one page.
        """
        with self._client() as client:
            response = self._transport.run(
                lambda: client.post(f"{self._BASE}/search", json={"page_size": 1}), idempotent=True
            )
            self._errors.raise_for(response, target="the integration's shared objects")
            return bool(cast("RawAPIDict", response.json()).get("results"))

    # ── page + database reads ───────────────────────────────────────────

    def get_page(self, page_id: str) -> RawAPIDict:
        with self._client() as client:
            response = self._transport.run(lambda: client.get(f"{self._BASE}/pages/{page_id}"), idempotent=True)
            self._errors.raise_for(response, target=f"page {page_id}")
            return cast("RawAPIDict", response.json())

    def get_database(self, database_id: str) -> RawAPIDict:
        with self._client() as client:
            response = self._transport.run(lambda: client.get(f"{self._BASE}/databases/{database_id}"), idempotent=True)
            self._errors.raise_for(response, target=f"database {database_id}")
            return cast("RawAPIDict", response.json())

    def get_block(self, block_id: str) -> RawAPIDict:
        with self._client() as client:
            response = self._transport.run(lambda: client.get(f"{self._BASE}/blocks/{block_id}"), idempotent=True)
            self._errors.raise_for(response, target=f"block {block_id}")
            return cast("RawAPIDict", response.json())

    def get_page_status(self, page_id: str, *, property_name: str = "Status") -> str | None:
        properties = self.get_page(page_id).get("properties")
        if not isinstance(properties, dict):
            return None
        return option_name(cast("RawAPIDict", properties).get(property_name))

    # ── liveness ────────────────────────────────────────────────────────

    def page_liveness(self, page_id: str) -> LivenessVerdict:
        """Whether *page_id* is still the LIVE version of itself.

        The primitives above stay raw — the probe itself needs an ungated
        ``get_page``, and so does an audit read. Every surface that hands an
        ANSWER to a human or an agent gates on this instead.
        """
        return PageLivenessProbe(self).verdict(page_id)

    def page_is_live(self, page_id: str) -> bool:
        """The boolean :class:`~teatree.core.backend_registry.NotionPageClient` exposes to core.

        UNKNOWN answers ``False``: a liveness this surface could not establish is
        not a liveness it may act on.
        """
        return self.page_liveness(page_id).readable

    def query_database(
        self, database_id: str, *, db_filter: RawAPIDict | None = None, page_size: int = 100, max_rows: int = 0
    ) -> list[RawAPIDict]:
        return self._query(
            f"databases/{database_id}/query", db_filter=db_filter, page_size=page_size, version="", max_rows=max_rows
        )

    def list_data_sources(self, database_id: str) -> list[RawAPIDict]:
        """The data sources of *database_id*, the only route to a modern database's rows.

        Sends :data:`DATA_SOURCE_API_VERSION`, under which every database reports
        its sources; the pinned default answers ``400`` on one that has them. An
        EMPTY list is meaningful rather than degenerate — it is how Notion says
        this integration was never granted the database's data.
        """
        with self._client() as client:
            response = self._transport.run(
                lambda: client.get(
                    f"{self._BASE}/databases/{database_id}",
                    headers={"Notion-Version": DATA_SOURCE_API_VERSION},
                ),
                idempotent=True,
            )
            self._errors.raise_for(response, target=f"database {database_id}")
            sources = cast("RawAPIDict", response.json()).get("data_sources")
        return cast("list[RawAPIDict]", sources) if isinstance(sources, list) else []

    def query_data_source(
        self, data_source_id: str, *, db_filter: RawAPIDict | None = None, page_size: int = 100, max_rows: int = 0
    ) -> list[RawAPIDict]:
        """Query a data source — the multi-source successor to a database query.

        Sends :data:`DATA_SOURCE_API_VERSION` on this request alone, because the
        endpoint does not exist under the pinned default and a caller holding a
        ``collection://`` data-source id has nothing else to point at.
        """
        return self._query(
            f"data_sources/{data_source_id}/query",
            db_filter=db_filter,
            page_size=page_size,
            version=DATA_SOURCE_API_VERSION,
            max_rows=max_rows,
        )

    def _paginate(self, request: PagedRequest, *, target: str, max_rows: int = 0) -> list[RawAPIDict]:
        """Follow ``next_cursor`` to the end of *request* — or to *max_rows* when set — one page at a time.

        A cursor Notion hands back a second time means the walk has stopped
        advancing; the read fails loud rather than spinning forever inside an
        unattended run.
        """
        results: list[RawAPIDict] = []
        cursor: str | None = None
        seen: set[str] = set()
        with self._client() as client:
            while True:
                response = self._transport.run(lambda c=cursor: request(client, c), idempotent=True)
                self._errors.raise_for(response, target=target)
                body = response.json()
                results.extend(body.get("results", []))
                cursor = body.get("next_cursor")
                if max_rows and len(results) >= max_rows:
                    return results[:max_rows]
                if not body.get("has_more") or not cursor:
                    return results
                if cursor in seen:
                    msg = (
                        f"Notion repeated the pagination cursor {cursor!r} while reading {target}; "
                        "the walk is not advancing, so the read is abandoned rather than looped forever."
                    )
                    raise NotionError(msg)
                seen.add(cursor)

    def _query(
        self, path: str, *, db_filter: RawAPIDict | None, page_size: int, version: str, max_rows: int = 0
    ) -> list[RawAPIDict]:
        headers = {"Notion-Version": version} if version else None

        def request(client: httpx.Client, cursor: str | None) -> httpx.Response:
            payload: RawAPIDict = {"page_size": page_size}
            if db_filter is not None:
                payload["filter"] = db_filter
            if cursor:
                payload["start_cursor"] = cursor
            return client.post(f"{self._BASE}/{path}", json=payload, headers=headers)

        return self._paginate(request, target=path, max_rows=max_rows)

    # ── block reads ─────────────────────────────────────────────────────

    def list_block_children(self, block_id: str) -> list[RawAPIDict]:
        """Return every direct child block of *block_id*, following pagination."""

        def request(client: httpx.Client, cursor: str | None) -> httpx.Response:
            params: dict[str, str] = {"page_size": "100"}
            if cursor:
                params["start_cursor"] = cursor
            return client.get(f"{self._BASE}/blocks/{block_id}/children", params=params)

        return self._paginate(request, target=f"block {block_id}")

    def list_comments(self, block_id: str) -> list[RawAPIDict]:
        """Return the open (unresolved) comments attached to *block_id*.

        Notion exposes only unresolved discussions on this endpoint and requires
        the integration's read-comment capability — an integration without it
        gets HTTP 403, reported as
        :class:`~teatree.backends.notion.errors.NotionCapabilityDeniedError` rather
        than as an empty comment list.
        """

        def request(client: httpx.Client, cursor: str | None) -> httpx.Response:
            params: dict[str, str] = {"block_id": block_id, "page_size": "100"}
            if cursor:
                params["start_cursor"] = cursor
            return client.get(f"{self._BASE}/comments", params=params)

        return self._paginate(request, target=f"comments on {block_id}")

    # ── block writes ────────────────────────────────────────────────────

    # ── guarded writes ──────────────────────────────────────────────────

    def check_writable(self, target: str, *, archived: bool = False) -> None:
        """The write guard's verdict on *target* without writing — what a dry run must refuse on."""
        if archived:
            self._write_guard.check_archiveable(target)
        else:
            self._write_guard.check(target)

    def _write(
        self,
        target: str,
        method: str,
        path: str,
        payload: RawAPIDict | None,
        *,
        described: str,
    ) -> RawAPIDict:
        """The one path a mutating request takes, so no write reaches Notion without the guard's verdict."""
        self.check_writable(target, archived=method.upper() == "DELETE" or _archives_or_trashes(payload))
        with self._client() as client:
            response = self._transport.run(
                lambda: client.request(method, f"{self._BASE}/{path}", json=payload), idempotent=False
            )
            self._errors.raise_for(response, target=described)
            return cast("RawAPIDict", response.json())

    def _parent_of(self, object_id: str) -> str | None:
        with self._client() as client:
            response = self._transport.run(lambda: client.get(f"{self._BASE}/blocks/{object_id}"), idempotent=True)
            self._errors.raise_for(response, target=f"block {object_id}")
            parent = cast("RawAPIDict", response.json().get("parent") or {})
        kind = parent.get("type")
        if kind == "workspace":
            return None
        linked = parent.get("database_id" if kind == "data_source_id" else str(kind))
        if not isinstance(linked, str) or not linked:
            msg = f"block {object_id} names no parent the write guard can follow ({kind!r})"
            raise NotionError(msg)
        return linked

    def append_block_children(self, block_id: str, children: list[RawAPIDict], *, after: str = "") -> list[RawAPIDict]:
        """Append *children* under *block_id*, batched to Notion's per-call cap.

        ``after`` inserts immediately following that sibling block instead of at
        the end — the primitive that lets a section body be rewritten under its
        own heading without disturbing anything below it. Each batch chains onto
        the last block the previous batch created, so a body longer than
        :data:`APPEND_BATCH_SIZE` still lands in order.
        """
        appended: list[RawAPIDict] = []
        anchor = after
        for start in range(0, len(children), APPEND_BATCH_SIZE):
            batch = children[start : start + APPEND_BATCH_SIZE]
            created = self._append_batch(block_id, batch, after=anchor)
            appended.extend(created)
            anchor = str(created[-1].get("id", "")) if created else anchor
        return appended

    def _append_batch(self, block_id: str, children: list[RawAPIDict], *, after: str) -> list[RawAPIDict]:
        payload: RawAPIDict = {"children": children}
        if after:
            payload["after"] = after
        body = self._write(block_id, "PATCH", f"blocks/{block_id}/children", payload, described=f"block {block_id}")
        return cast("list[RawAPIDict]", body.get("results", []))

    def update_block(self, block_id: str, payload: RawAPIDict) -> RawAPIDict:
        """Patch one block in place, preserving its id and its discussions."""
        return self._write(block_id, "PATCH", f"blocks/{block_id}", payload, described=f"block {block_id}")

    def delete_block(self, block_id: str) -> RawAPIDict:
        """Archive one block (Notion's ``DELETE`` is a move to trash, not a purge)."""
        return self._write(block_id, "DELETE", f"blocks/{block_id}", None, described=f"block {block_id}")

    def update_page(self, page_id: str, payload: RawAPIDict) -> RawAPIDict:
        """Patch one page's properties or ``archived`` flag — needs the integration's update-content capability."""
        return self._write(page_id, "PATCH", f"pages/{page_id}", payload, described=f"page {page_id}")

    def create_page(
        self, parent_id: str, *, parent: RawAPIDict, properties: RawAPIDict, children: list[RawAPIDict], icon: str
    ) -> RawAPIDict:
        """Create a page under *parent_id*; the guard judges the parent's chain, which the new page joins."""
        payload: RawAPIDict = {"parent": parent, "properties": properties, "children": children}
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        return self._write(parent_id, "POST", "pages", payload, described=f"a new page under {parent_id}")

    # ── comment writes ──────────────────────────────────────────────────

    def post_comment(self, target: str, where: RawAPIDict, comment_rich_text: list[RawAPIDict]) -> RawAPIDict:
        """Post a comment placed by *where* (a page or block parent, or a discussion id); the guard judges *target*.

        The API anchors a new discussion on a page or on a whole block, never on a selected
        text range; a reply names its ``discussion_id``, and *target* is the object that
        discussion is anchored on.
        """
        return self._write(
            target, "POST", "comments", {**where, "rich_text": comment_rich_text}, described=f"comments on {target}"
        )
