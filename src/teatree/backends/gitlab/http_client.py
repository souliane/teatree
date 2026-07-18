"""GitLab REST/GraphQL transport — the raw HTTP concern (#3235 module-health split).

Split out of :mod:`teatree.backends.gitlab.api` so the TRANSPORT (auth, retries,
offset pagination, the TTL response cache, uploads, GraphQL) lives apart from the
DOMAIN queries (:class:`~teatree.backends.gitlab.api.GitLabAPI`'s MR / issue /
pipeline reads) that are layered on top of it. ``api`` re-exports every name below,
so existing importers are unchanged.

Two reliability invariants govern every request:

*   **Fail loud on a missing credential.** A request with no resolved token
    raises :class:`~teatree.core.backend_protocols.BackendResolutionError` rather
    than returning ``None`` / ``[]`` / ``0`` — a credential outage must never be
    indistinguishable from "the API returned nothing" (which would let, e.g., a
    merge-authorising approval read silently degrade to an empty answer).
*   **Bounded retry on transient failures.** Naked ``httpx`` calls broke a whole
    loop tick on a single transient ``502`` / ``429`` / connect timeout. Every
    request now runs under :meth:`GitLabHTTPClient._run` — a bounded exponential
    backoff that honours ``Retry-After`` and is gated by both the failure class
    and the call's idempotency, mirroring
    :class:`teatree.backends.slack.http.SlackHttpClient`.
"""

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from teatree.core.backend_protocols import BackendResolutionError
from teatree.llm.credentials import Credential, CredentialError, CredentialSpec

logger = logging.getLogger(__name__)

type RawMR = dict[str, object]
"""One raw GitLab JSON object as the REST API returns it (MR, issue, note, upload...).

The transport cannot know the shape of an arbitrary endpoint's payload, so the named
alias IS the typed contract here: "an untyped JSON object straight off the wire".
Callers narrow it at the domain layer (``api.GitLabAPI``) or at the host boundary.
"""

type AttemptFn = Callable[[], httpx.Response]
type SleepFn = Callable[[float], None]

# Upper bound on pages walked for an offset-paginated list endpoint. GitLab
# serves at most 100 items per page; this cap stops a runaway loop if the API
# ever returns a malformed ``x-next-page`` that never empties.
_MAX_PAGES = 100

_DEFAULT_TIMEOUT_SECONDS = 10.0
_UPLOAD_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 30.0
# Statuses at or above which a write is a failure the caller may drop on the floor;
# the transport logs it so a failed MR update / note post never vanishes silently.
_HTTP_ERROR_FLOOR = 400
_CONNECT_ERRORS = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    project_id: int
    path_with_namespace: str
    short_name: str
    default_branch: str = "main"


class GitLabTokenCredential(Credential):
    """The GitLab PAT — resolved env-first, ``pass``-fallback, through the audited seam.

    Routes GitLab-token resolution through the provider-neutral
    :class:`~teatree.llm.credentials.Credential` machinery (env wins, then the
    ``pass`` store) instead of an ad-hoc ``os.environ.get`` + ``read_pass`` pair,
    so the lookup is testable with injected sources and a rotated ``GITLAB_TOKEN``
    env value always overrides a stale ``pass`` entry. Unlike the metered Claude
    credentials this keeps its documented ``pass`` default (``gitlab/pat``) so
    existing local setups keep working; the fail-loud-on-empty guarantee is
    enforced at the transport boundary via :meth:`GitLabHTTPClient._require_token`.
    """

    spec = CredentialSpec(
        env_var="GITLAB_TOKEN",
        conflicting_vars=(),
        pass_path="gitlab/pat",  # noqa: S106 — pass entry path, not a secret value
    )


def _resolve_token() -> str:
    """Resolve a GitLab token (env wins, then ``pass``), or ``""`` when neither yields one.

    Returns ``""`` (rather than raising) so a tokenless :class:`GitLabHTTPClient`
    can still be CONSTRUCTED — the loud failure is deferred to the point of use
    (:meth:`GitLabHTTPClient._require_token`), which lets a resolve-or-skip caller
    check ``.token`` presence before issuing any request.
    """
    try:
        return GitLabTokenCredential().resolve()
    except CredentialError:
        return ""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class GitLabHTTPClient:
    def __init__(
        self,
        *,
        token: str = "",
        base_url: str = "https://gitlab.com/api/v4",
        max_retries: int | None = None,
        backoff_base: float | None = None,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self.token = token or _resolve_token()
        self.base_url = base_url.rstrip("/")
        self._project_cache: dict[str, ProjectInfo] = {}
        self._response_cache: dict[str, tuple[float, object]] = {}
        self._timeout = _env_float("T3_GITLAB_HTTP_TIMEOUT", _DEFAULT_TIMEOUT_SECONDS)
        self._max_retries = (
            max_retries if max_retries is not None else _env_int("T3_GITLAB_HTTP_MAX_RETRIES", _DEFAULT_MAX_RETRIES)
        )
        self._backoff_base = (
            backoff_base
            if backoff_base is not None
            else _env_float("T3_GITLAB_HTTP_BACKOFF", _DEFAULT_BACKOFF_BASE_SECONDS)
        )
        self._sleep = sleep

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

    def _require_token(self) -> None:
        """Raise :class:`BackendResolutionError` when no GitLab credential resolved.

        The fail-loud boundary: a missing token is an outage, never an empty
        result. Surfacing it here keeps a credential gap distinguishable from a
        genuine "the API returned nothing" — the failure mode where a silent
        ``None`` / ``[]`` / ``0`` fed a merge/notify decision as if it were data.
        """
        if not self.token:
            msg = (
                "GitLab request attempted with no resolved credential. Set GITLAB_TOKEN "
                "in the environment or store one with `pass insert gitlab/pat`."
            )
            raise BackendResolutionError(msg)

    def clear_response_cache(self) -> None:
        self._response_cache.clear()

    def _run(self, attempt: AttemptFn, *, idempotent: bool) -> httpx.Response:
        """Execute *attempt* under a bounded retry, returning the ``httpx.Response``.

        Retries are gated by BOTH the failure class and the call's idempotency,
        mirroring :class:`teatree.backends.slack.http.SlackHttpClient`:

        *   a CONNECT-phase failure (the request never reached GitLab) is safe to
            retry for any call, idempotent or not — nothing was sent;
        *   a RESPONSE-phase failure (a read timeout, a ``5xx``, a ``429``) is
            retried only for an idempotent call, so a non-idempotent ``POST``
            (creating a note / pipeline) is never blindly replayed.

        A ``429`` / ``5xx`` honours the ``Retry-After`` header when present, else
        the standard exponential backoff. The response is returned WITHOUT
        ``raise_for_status`` so status-inspecting callers (``post_status`` et al.)
        keep working; body-returning callers apply ``raise_for_status`` themselves.
        """
        for retries_left in range(self._max_retries, -1, -1):
            last = retries_left == 0
            try:
                response = attempt()
            except _CONNECT_ERRORS:
                if last:
                    raise
                self._backoff(self._max_retries - retries_left)
                continue
            except httpx.TimeoutException:
                if last or not idempotent:
                    raise
                self._backoff(self._max_retries - retries_left)
                continue
            retry_after = self._transient_response_wait(response, idempotent=idempotent)
            if retry_after is None or last:
                return response
            self._sleep_for(retry_after if retry_after > 0 else self._backoff_seconds(self._max_retries - retries_left))
        unreachable = "retry loop is exhaustive: the final iteration always returns or raises"
        raise AssertionError(unreachable)  # pragma: no cover

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    def _get(self, endpoint: str, *, timeout: float | None = None) -> httpx.Response:
        url = self._url(endpoint)
        request_timeout = timeout if timeout is not None else self._timeout

        def attempt() -> httpx.Response:
            return httpx.get(url, headers=self._headers(), timeout=request_timeout)

        return self._run(attempt, idempotent=True)

    def _post(self, endpoint: str, *, json: object, idempotent: bool = False) -> httpx.Response:
        url = self._url(endpoint)

        def attempt() -> httpx.Response:
            return httpx.post(url, headers=self._headers(), json=json, timeout=self._timeout)

        return self._run(attempt, idempotent=idempotent)

    def _put(self, endpoint: str, *, json: object) -> httpx.Response:
        url = self._url(endpoint)

        def attempt() -> httpx.Response:
            return httpx.put(url, headers=self._headers(), json=json, timeout=self._timeout)

        # PUT is idempotent by HTTP semantics — safe to replay on a response-phase failure.
        return self._run(attempt, idempotent=True)

    def _delete(self, endpoint: str) -> httpx.Response:
        url = self._url(endpoint)

        def attempt() -> httpx.Response:
            return httpx.delete(url, headers=self._headers(), timeout=self._timeout)

        return self._run(attempt, idempotent=True)

    def _transient_response_wait(self, response: httpx.Response, *, idempotent: bool) -> float | None:
        """Seconds to wait before a response-phase retry, or ``None`` when not retryable.

        ``0.0`` means "retry on the standard backoff"; a positive value is an
        explicit ``Retry-After`` to honour. ``None`` means surface the response to
        the caller (success, a non-transient status, or a non-idempotent write
        that must not be replayed on a response-phase failure).
        """
        if not self._is_transient_response(response):
            return None
        if not idempotent:
            return None
        return self._retry_after_seconds(response)

    @staticmethod
    def _is_transient_response(response: httpx.Response) -> bool:
        # A real httpx.Response always carries an int status; a response object that
        # reports no integer status is not classifiable as transient (surfaced as-is).
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            return False
        return status >= httpx.codes.INTERNAL_SERVER_ERROR or status == httpx.codes.TOO_MANY_REQUESTS

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        header = response.headers.get("Retry-After", "").strip()
        if not header:
            return 0.0
        try:
            return max(0.0, float(header))
        except ValueError:
            return 0.0

    def _backoff(self, attempt_index: int) -> None:
        self._sleep_for(self._backoff_seconds(attempt_index))

    def _backoff_seconds(self, attempt_index: int) -> float:
        return min(_MAX_BACKOFF_SECONDS, self._backoff_base * (2**attempt_index))

    def _sleep_for(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(min(_MAX_BACKOFF_SECONDS, seconds))

    @staticmethod
    def _log_write_failure(method: str, endpoint: str, status: int) -> int:
        """Log a warning when a status-returning write failed, then return the status.

        Status-returning helpers (``post_status`` / ``put_status`` / ``delete``)
        hand the raw code back so callers keep their 2xx/non-2xx branching; the
        warning ensures a failed write is never *silently* ignored even when a
        caller drops the return value.
        """
        if status >= _HTTP_ERROR_FLOOR:
            logger.warning("GitLab %s %s failed with HTTP %s", method, endpoint, status)
        return status

    def get_json(self, endpoint: str) -> RawMR | list[RawMR] | None:
        self._require_token()
        response = self._get(endpoint)
        response.raise_for_status()
        return cast("RawMR | list[RawMR]", response.json())

    def get_json_paginated(self, endpoint: str) -> list[RawMR]:
        """Fetch every page of an offset-paginated GitLab list endpoint.

        GitLab returns each list page's continuation in the ``x-next-page``
        response header — the next page number, or empty on the last page.
        ``get_json`` reads only the first page, silently truncating any result
        set larger than ``per_page``; this follows ``x-next-page`` until empty,
        accumulating every page's items. *endpoint* should already carry the
        query string; the ``page`` parameter is appended per request. Raises
        :class:`BackendResolutionError` when no token resolved.
        """
        self._require_token()
        sep = "&" if "?" in endpoint else "?"
        items: list[RawMR] = []
        page = 1
        for _ in range(_MAX_PAGES):
            response = self._get(f"{endpoint.lstrip('/')}{sep}page={page}")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, list):
                break
            items.extend(cast("list[RawMR]", body))
            next_page = self._parse_next_page(response.headers.get("x-next-page", ""))
            if next_page is None:
                break
            page = next_page
        else:
            logger.warning(
                "GitLab pagination hit the %s-page cap for %s — result may be truncated", _MAX_PAGES, endpoint
            )
        return items

    @staticmethod
    def _parse_next_page(raw: str) -> int | None:
        """The next page number from an ``x-next-page`` header, or ``None`` to stop.

        GitLab sends an empty header on the last page; a malformed non-numeric
        value (which ``int()`` would crash on) is treated as "no further page"
        and logged, so a broken header ends pagination cleanly rather than
        raising mid-walk.
        """
        raw = raw.strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            logger.warning("GitLab returned a non-numeric x-next-page header %r — stopping pagination", raw)
            return None

    def post_json(self, endpoint: str, payload: RawMR | None = None) -> RawMR | None:
        self._require_token()
        response = self._post(endpoint, json=payload or {})
        response.raise_for_status()
        return cast("RawMR", response.json())

    def post_status(self, endpoint: str, payload: Mapping[str, object] | None = None) -> int:
        self._require_token()
        response = self._post(endpoint, json=dict(payload) if payload else {})
        return self._log_write_failure("POST", endpoint, response.status_code)

    def put_json(self, endpoint: str, payload: RawMR | None = None) -> RawMR | None:
        self._require_token()
        response = self._put(endpoint, json=payload or {})
        response.raise_for_status()
        return cast("RawMR", response.json())

    def put_status(self, endpoint: str, payload: Mapping[str, object] | None = None) -> int:
        self._require_token()
        response = self._put(endpoint, json=dict(payload) if payload else {})
        return self._log_write_failure("PUT", endpoint, response.status_code)

    def delete(self, endpoint: str) -> int:
        self._require_token()
        response = self._delete(endpoint)
        return self._log_write_failure("DELETE", endpoint, response.status_code)

    def upload_file(self, project_id: int, filepath: str) -> RawMR | None:
        self._require_token()
        with Path(filepath).open("rb") as f:
            response = httpx.post(
                self._url(f"projects/{project_id}/uploads"),
                headers=self._headers(),
                files={"file": (Path(filepath).name, f)},
                timeout=_UPLOAD_TIMEOUT_SECONDS,
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
        Raises :class:`BackendResolutionError` when no token resolved.
        """
        self._require_token()
        response = self._get(f"projects/{project_id}/uploads/{secret}/{filename}", timeout=_UPLOAD_TIMEOUT_SECONDS)
        return response.status_code, response.content

    def graphql(self, query: str, variables: RawMR | None = None) -> RawMR | None:
        self._require_token()
        graphql_url = self.base_url.replace("/api/v4", "/api/graphql")

        def attempt() -> httpx.Response:
            return httpx.post(
                graphql_url,
                headers=self._headers(),
                json={"query": query, "variables": variables or {}},
                timeout=self._timeout,
            )

        # A GraphQL POST can carry a mutation, so it is treated as non-idempotent
        # (never replayed on a response-phase failure); a transport-level connect
        # failure is still safely retried.
        response = self._run(attempt, idempotent=False)
        response.raise_for_status()
        return cast("RawMR", response.json())
