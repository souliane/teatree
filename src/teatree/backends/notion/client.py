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
missing page.

Reads run under the shared bounded-retry transport
(:class:`~teatree.backends.http_retry.SimpleRetryTransport`, knobs from
``T3_NOTION_HTTP_*``); every mutation is non-idempotent and is retried only on a
CONNECT-phase failure, never replayed once the request reached Notion.
"""

from typing import cast

import httpx

from teatree.backends.http_retry import SimpleRetryTransport
from teatree.backends.notion.errors import NotionBadTokenError, NotionErrorClassifier
from teatree.backends.notion.liveness import LivenessVerdict, PageLivenessProbe
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


class NotionTokenCredential(Credential):
    """The Notion internal-integration token — env first, then the ``pass`` store.

    Routes through the provider-neutral :class:`~teatree.llm.credentials.Credential`
    machinery (identical to ``FigmaTokenCredential``) so a rotated ``NOTION_TOKEN``
    always beats a stale ``pass`` entry, the value never reaches argv, and an
    absent credential fails loud naming the fix instead of authenticating as
    nothing. An overlay routes its own entry by injecting ``pass_path_override``.
    """

    spec = CredentialSpec(
        env_var="NOTION_TOKEN",
        conflicting_vars=(),
        pass_path="notion/integration-token",  # noqa: S106 — a `pass` entry path, not a secret value
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


class NotionClient:
    """Notion API client — pages, blocks, comments, databases, status writes."""

    _BASE = "https://api.notion.com/v1"

    def __init__(self, *, token: str, version: str = DEFAULT_API_VERSION) -> None:
        self.token = token
        self.version = version
        self._transport = SimpleRetryTransport(env_prefix="T3_NOTION_HTTP")
        self._errors = NotionErrorClassifier(self.describe_identity)

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

    # ── page + database reads ───────────────────────────────────────────

    def get_page(self, page_id: str) -> RawAPIDict:
        with self._client() as client:
            response = self._transport.run(lambda: client.get(f"{self._BASE}/pages/{page_id}"), idempotent=True)
            self._errors.raise_for(response, target=f"page {page_id}")
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
        self, database_id: str, *, db_filter: RawAPIDict | None = None, page_size: int = 100
    ) -> list[RawAPIDict]:
        return self._query(f"databases/{database_id}/query", db_filter=db_filter, page_size=page_size, version="")

    def query_data_source(
        self, data_source_id: str, *, db_filter: RawAPIDict | None = None, page_size: int = 100
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
        )

    def _query(self, path: str, *, db_filter: RawAPIDict | None, page_size: int, version: str) -> list[RawAPIDict]:
        results: list[RawAPIDict] = []
        cursor: str | None = None
        headers = {"Notion-Version": version} if version else None
        with self._client() as client:
            while True:
                payload: RawAPIDict = {"page_size": page_size}
                if db_filter is not None:
                    payload["filter"] = db_filter
                if cursor:
                    payload["start_cursor"] = cursor
                response = self._transport.run(
                    lambda p=payload: client.post(f"{self._BASE}/{path}", json=p, headers=headers),
                    idempotent=True,
                )
                self._errors.raise_for(response, target=path)
                body = response.json()
                results.extend(body.get("results", []))
                cursor = body.get("next_cursor")
                if not body.get("has_more") or not cursor:
                    return results

    # ── block reads ─────────────────────────────────────────────────────

    def list_block_children(self, block_id: str) -> list[RawAPIDict]:
        """Return every direct child block of *block_id*, following pagination."""
        results: list[RawAPIDict] = []
        cursor: str | None = None
        with self._client() as client:
            while True:
                params: dict[str, str] = {"page_size": "100"}
                if cursor:
                    params["start_cursor"] = cursor
                response = self._transport.run(
                    lambda p=params: client.get(f"{self._BASE}/blocks/{block_id}/children", params=p),
                    idempotent=True,
                )
                self._errors.raise_for(response, target=f"block {block_id}")
                body = response.json()
                results.extend(body.get("results", []))
                cursor = body.get("next_cursor")
                if not body.get("has_more") or not cursor:
                    return results

    def list_comments(self, block_id: str) -> list[RawAPIDict]:
        """Return the open (unresolved) comments attached to *block_id*.

        Notion exposes only unresolved discussions on this endpoint and requires
        the integration's read-comment capability — an integration without it
        gets HTTP 403, reported as
        :class:`~teatree.backends.notion.errors.NotionCapabilityDeniedError` rather
        than as an empty comment list.
        """
        results: list[RawAPIDict] = []
        cursor: str | None = None
        with self._client() as client:
            while True:
                params: dict[str, str] = {"block_id": block_id, "page_size": "100"}
                if cursor:
                    params["start_cursor"] = cursor
                response = self._transport.run(
                    lambda p=params: client.get(f"{self._BASE}/comments", params=p), idempotent=True
                )
                self._errors.raise_for(response, target=f"comments on {block_id}")
                body = response.json()
                results.extend(body.get("results", []))
                cursor = body.get("next_cursor")
                if not body.get("has_more") or not cursor:
                    return results

    # ── block writes ────────────────────────────────────────────────────

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
        with self._client() as client:
            response = self._transport.run(
                lambda: client.patch(f"{self._BASE}/blocks/{block_id}/children", json=payload),
                idempotent=False,
            )
            self._errors.raise_for(response, target=f"block {block_id}")
            body = response.json()
        return cast("list[RawAPIDict]", body.get("results", []))

    def update_block(self, block_id: str, payload: RawAPIDict) -> RawAPIDict:
        """Patch one block in place, preserving its id and its discussions."""
        with self._client() as client:
            response = self._transport.run(
                lambda: client.patch(f"{self._BASE}/blocks/{block_id}", json=payload), idempotent=False
            )
            self._errors.raise_for(response, target=f"block {block_id}")
            return cast("RawAPIDict", response.json())

    def delete_block(self, block_id: str) -> RawAPIDict:
        """Archive one block (Notion's ``DELETE`` is a move to trash, not a purge)."""
        with self._client() as client:
            response = self._transport.run(lambda: client.delete(f"{self._BASE}/blocks/{block_id}"), idempotent=False)
            self._errors.raise_for(response, target=f"block {block_id}")
            return cast("RawAPIDict", response.json())

    def update_page_properties(self, page_id: str, properties: RawAPIDict) -> RawAPIDict:
        """Patch named page properties — needs the integration's update-content capability."""
        with self._client() as client:
            response = self._transport.run(
                lambda: client.patch(f"{self._BASE}/pages/{page_id}", json={"properties": properties}),
                idempotent=False,
            )
            self._errors.raise_for(response, target=f"page {page_id}")
            return cast("RawAPIDict", response.json())

    def update_page_status(self, page_id: str, *, property_name: str, value: str) -> RawAPIDict:
        return self.update_page_properties(page_id, {property_name: {"status": {"name": value}}})

    # ── comment writes ──────────────────────────────────────────────────

    def create_comment(self, page_id: str, comment_rich_text: list[RawAPIDict]) -> RawAPIDict:
        """Open a new discussion on *page_id* — needs the insert-comment capability.

        Page-scoped on purpose. Notion exposes unresolved comments per BLOCK, so
        a reply into a discussion anchored on a child block could not be read
        back from the page — and a write this surface cannot re-read is a write
        it cannot verify.
        """
        with self._client() as client:
            response = self._transport.run(
                lambda: client.post(
                    f"{self._BASE}/comments",
                    json={"parent": {"page_id": page_id}, "rich_text": comment_rich_text},
                ),
                idempotent=False,
            )
            self._errors.raise_for(response, target=f"comments on page {page_id}")
            return cast("RawAPIDict", response.json())
