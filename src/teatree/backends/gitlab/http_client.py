"""GitLab REST/GraphQL transport — the raw HTTP concern (#3235 module-health split).

Split out of :mod:`teatree.backends.gitlab.api` so the TRANSPORT (auth, retries,
offset pagination, the TTL response cache, uploads, GraphQL) lives apart from the
DOMAIN queries (:class:`~teatree.backends.gitlab.api.GitLabAPI`'s MR / issue /
pipeline reads) that are layered on top of it. ``api`` re-exports every name below,
so existing importers are unchanged.
"""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

type RawMR = dict[str, object]
"""One raw GitLab JSON object as the REST API returns it (MR, issue, note, upload...).

The transport cannot know the shape of an arbitrary endpoint's payload, so the named
alias IS the typed contract here: "an untyped JSON object straight off the wire".
Callers narrow it at the domain layer (``api.GitLabAPI``) or at the host boundary.
"""

# Upper bound on pages walked for an offset-paginated list endpoint. GitLab
# serves at most 100 items per page; this cap stops a runaway loop if the API
# ever returns a malformed ``x-next-page`` that never empties.
_MAX_PAGES = 100


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    project_id: int
    path_with_namespace: str
    short_name: str
    default_branch: str = "main"


def _resolve_token() -> str:
    """Resolve a GitLab token from env, then ``pass`` store as fallback."""
    token = os.environ.get("GITLAB_TOKEN", "")
    if token:
        return token
    from teatree.utils.secrets import read_pass  # noqa: PLC0415 — deferred to avoid circular import at module load

    return read_pass("gitlab/pat")


class GitLabHTTPClient:
    def __init__(self, *, token: str = "", base_url: str = "https://gitlab.com/api/v4") -> None:
        self.token = token or _resolve_token()
        self.base_url = base_url.rstrip("/")
        self._project_cache: dict[str, ProjectInfo] = {}
        self._response_cache: dict[str, tuple[float, object]] = {}

    def _get_cached[T](self, cache_key: str, ttl: int) -> T | None:
        """Return the fresh cached value for *cache_key*, or ``None`` when missing/stale.

        The cache is heterogeneous (each ``get_*`` method stores its own return
        shape under a prefixed key), so the stored value is ``object``. The
        caller binds ``T`` at the call site by annotating the receiving local —
        every writer/reader pair for a given key agrees on the shape — which
        confines the unavoidable dynamic hop to this one ``cast`` instead of a
        per-call-site suppression at each of the seven cache-hit returns.
        """
        entry = self._response_cache.get(cache_key)
        if entry is not None and (time.monotonic() - entry[0]) < ttl:
            return cast("T", entry[1])
        return None

    def _set_cached(self, cache_key: str, value: object) -> None:
        self._response_cache[cache_key] = (time.monotonic(), value)

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def clear_response_cache(self) -> None:
        self._response_cache.clear()

    def get_json(self, endpoint: str) -> RawMR | list[RawMR] | None:
        if not self.token:
            return None
        response = httpx.get(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawMR | list[RawMR]", response.json())

    def get_json_paginated(self, endpoint: str) -> list[RawMR]:
        """Fetch every page of an offset-paginated GitLab list endpoint.

        GitLab returns each list page's continuation in the ``x-next-page``
        response header — the next page number, or empty on the last page.
        ``get_json`` reads only the first page, silently truncating any result
        set larger than ``per_page``; this follows ``x-next-page`` until empty,
        accumulating every page's items. Returns an empty list when there is no
        token or a page body is not a JSON array. *endpoint* should already
        carry the query string; the ``page`` parameter is appended per request.
        """
        if not self.token:
            return []
        sep = "&" if "?" in endpoint else "?"
        items: list[RawMR] = []
        page = 1
        for _ in range(_MAX_PAGES):
            response = httpx.get(
                f"{self.base_url}/{endpoint.lstrip('/')}{sep}page={page}",
                headers=self._headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, list):
                break
            items.extend(cast("list[RawMR]", body))
            next_page = response.headers.get("x-next-page", "")
            if not next_page:
                break
            page = int(next_page)
        return items

    def post_json(self, endpoint: str, payload: RawMR | None = None) -> RawMR | None:
        if not self.token:
            return None
        response = httpx.post(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=payload or {},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawMR", response.json())

    def post_status(self, endpoint: str, payload: Mapping[str, object] | None = None) -> int:
        if not self.token:
            return 0
        response = httpx.post(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=dict(payload) if payload else {},
            timeout=10.0,
        )
        return response.status_code

    def put_json(self, endpoint: str, payload: RawMR | None = None) -> RawMR | None:
        if not self.token:
            return None
        response = httpx.put(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=payload or {},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawMR", response.json())

    def put_status(self, endpoint: str, payload: Mapping[str, object] | None = None) -> int:
        if not self.token:
            return 0
        response = httpx.put(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=dict(payload) if payload else {},
            timeout=10.0,
        )
        return response.status_code

    def delete(self, endpoint: str) -> int:
        if not self.token:
            return 0
        response = httpx.delete(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            timeout=10.0,
        )
        return response.status_code

    def upload_file(self, project_id: int, filepath: str) -> RawMR | None:
        if not self.token:
            return None
        with Path(filepath).open("rb") as f:
            response = httpx.post(
                f"{self.base_url}/projects/{project_id}/uploads",
                headers=self._headers(),
                files={"file": (Path(filepath).name, f)},
                timeout=30.0,
            )
        response.raise_for_status()
        return cast("RawMR", response.json())

    def fetch_upload(self, project_id: int, secret: str, filename: str) -> tuple[int, bytes]:
        """Fetch an uploaded file's bytes through the token-authenticated API route.

        The web upload-serving routes (``/uploads/<secret>/<file>`` and the
        ``/-/project/<id>/uploads/...`` form a rendered note's ``<img>``/``<video>``
        points at) reject a ``PRIVATE-TOKEN`` — they require a browser session
        cookie (a token request 302s to sign-in or 404s). The API route
        ``GET /projects/:id/uploads/:secret/:filename`` (GitLab 16.6+) is the
        only token-authenticated way to confirm an upload resolves. Returns the
        HTTP status and the response body so the caller can assert ``200`` and
        magic-byte-check the content (GitLab serves every upload as
        ``application/octet-stream``, so the content-type header proves nothing).
        Returns ``(0, b"")`` when there is no token.
        """
        if not self.token:
            return 0, b""
        response = httpx.get(
            f"{self.base_url}/projects/{project_id}/uploads/{secret}/{filename}",
            headers=self._headers(),
            timeout=30.0,
        )
        return response.status_code, response.content

    def graphql(self, query: str, variables: RawMR | None = None) -> RawMR | None:
        if not self.token:
            return None
        graphql_url = self.base_url.replace("/api/v4", "/api/graphql")
        response = httpx.post(
            graphql_url,
            headers=self._headers(),
            json={"query": query, "variables": variables or {}},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawMR", response.json())
