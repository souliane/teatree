"""GitHub CLI and pooled API request helpers — the low-level transport concern.

Free functions wrapping ``gh`` and pooled API calls (GET / paginated GET / search /
POST / PATCH) plus the issue-URL parse and the HTTP 404 predicate, so
:class:`teatree.backends.github.GitHubCodeHost` stays focused on the cross-host
Protocol surface and under the module-health cap — the same split shape as
:mod:`teatree.backends.gitlab.api`.
"""

import json
import os
import re
import shutil
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

import httpx

from teatree.types import RawAPIDict
from teatree.utils.forge_http import client_for_url
from teatree.utils.run import CommandFailedError, CompletedProcess, run_allowed_to_fail, run_checked

_ISSUE_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")

# Bound each forge request and non-API ``gh`` subprocess so a network stall cannot wedge a loop.
_FORGE_READ_TIMEOUT_SECONDS = 60.0


def _run_gh(
    *args: str, token: str = "", timeout: float | None = None, stdin_text: str | None = None
) -> CompletedProcess[str]:
    """Route API calls through the host pool; keep non-API ``gh`` commands on the CLI."""
    if not token:
        raise CommandFailedError(
            list(args),
            4,
            "",
            "github_token_pass_key resolved no token; refusing ambient gh authentication",
        )
    if args[:2] == ("gh", "api"):
        return _run_pooled_api(args, token=token, timeout=timeout, stdin_text=stdin_text)
    if stdin_text is None:
        return run_checked(list(args), env=_gh_env(token), timeout=timeout)
    return run_checked(list(args), env=_gh_env(token), timeout=timeout, stdin_text=stdin_text)


def _api_url(endpoint: str) -> str:
    host = os.environ.get("GH_HOST", "github.com").strip().lower() or "github.com"
    base = "https://api.github.com" if host in {"github.com", "api.github.com"} else f"https://{host}/api/v3"
    return f"{base}/{endpoint.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class _ApiOptions:
    method: str
    headers: dict[str, str]
    fields: dict[str, str]
    paginate: bool
    slurp: bool
    silent: bool
    jq: str


def _parse_api_options(args: tuple[str, ...], token: str) -> _ApiOptions:
    method = "POST" if args[2] == "graphql" else "GET"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    fields: dict[str, str] = {}
    flags: set[str] = set()
    jq = ""
    index = 3
    while index < len(args):
        option = args[index]
        if option in {"--header", "--method", "--jq", "-f", "--field", "--input"}:
            value = args[index + 1]
            if option == "--header":
                name, _, content = value.partition(":")
                headers[name] = content.strip()
            elif option == "--method":
                method = value
            elif option == "--jq":
                jq = value
            elif option in {"-f", "--field"}:
                name, _, content = value.partition("=")
                fields[name] = content
            index += 2
            continue
        if option in {"--paginate", "--slurp", "--silent"}:
            flags.add(option)
            index += 1
            continue
        raise CommandFailedError(list(args), 2, "", f"Unsupported pooled gh api option: {option}")
    return _ApiOptions(method, headers, fields, "--paginate" in flags, "--slurp" in flags, "--silent" in flags, jq)


def _request_api(
    args: tuple[str, ...], url: str, options: _ApiOptions, *, timeout: float | None, body: str | None
) -> httpx.Response:
    try:
        headers = {**options.headers, "Content-Type": "application/json"} if body is not None else options.headers
        response = client_for_url(url).request(
            options.method,
            url,
            headers=headers,
            content=body,
            json=options.fields or None,
            timeout=timeout or _FORGE_READ_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CommandFailedError(list(args), 1, "", f"gh api: HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise CommandFailedError(list(args), 1, "", f"gh api: {type(exc).__name__}: {exc}") from exc
    return response


def _api_pages(args: tuple[str, ...], url: str, options: _ApiOptions, *, timeout: float | None) -> list[object]:
    pages: list[object] = []
    first_origin = str(httpx.URL(url).copy_with(path="", query=None, fragment=None))
    for _ in range(1000):
        response = _request_api(args, url, options, timeout=timeout, body=None)
        pages.append(response.json())
        next_url = response.links.get("next", {}).get("url")
        if not next_url:
            return pages
        if str(httpx.URL(next_url).copy_with(path="", query=None, fragment=None)) != first_origin:
            raise CommandFailedError(list(args), 1, "", "gh api: pagination crossed forge host")
        url = next_url
    raise CommandFailedError(list(args), 1, "", "gh api: pagination limit exceeded")


def _run_pooled_api(
    args: tuple[str, ...], *, token: str, timeout: float | None, stdin_text: str | None
) -> CompletedProcess[str]:
    endpoint = args[2]
    options = _parse_api_options(args, token)

    url = _api_url(endpoint)
    if endpoint == "graphql":
        url = _api_url("graphql").replace("/api/v3/graphql", "/api/graphql")
    if options.paginate:
        pages = _api_pages(args, url, options, timeout=timeout)
        stdout = json.dumps(pages if options.slurp else pages[-1])
    else:
        response = _request_api(args, url, options, timeout=timeout, body=stdin_text)
        stdout = str(response.json()["sha"]) + "\n" if options.jq == ".sha" else response.text
    return CompletedProcess(args=list(args), returncode=0, stdout="" if options.silent else stdout, stderr="")


def is_not_found(exc: CommandFailedError) -> bool:
    """Whether a failed ``gh api`` call was a genuine HTTP 404.

    ``gh api`` exits non-zero for EVERY HTTP error (404, 401, 403, 5xx alike)
    with returncode 1, so the only reliable signal for a permanent "no such
    resource" is the literal ``HTTP 404`` string in stderr. Everything else is
    an indeterminate failure the caller must NOT swallow as an empty result.
    """
    return "HTTP 404" in exc.stderr


def _gh_env(token: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITLAB_TOKEN", None)
    env["GH_TOKEN"] = token
    return env


def run_gh_unchecked(args: list[str], *, token: str) -> CompletedProcess[str]:
    argv = [shutil.which("gh") or "gh", *args]
    if not token:
        return CompletedProcess(args=argv, returncode=4, stdout="", stderr="github token unavailable")
    try:
        return run_allowed_to_fail(argv, expected_codes=None, env=_gh_env(token))
    except FileNotFoundError:
        return CompletedProcess(args=argv, returncode=127, stdout="", stderr="gh not installed")


def gh_api_get(endpoint: str, *, token: str = "") -> object:
    """Call ``gh api`` (GET) and return parsed JSON."""
    result = _run_gh(
        "gh",
        "api",
        endpoint,
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def gh_api_get_paginated(endpoint: str, *, token: str = "", strict: bool = False) -> list[RawAPIDict]:
    """Fetch EVERY page of a list endpoint and return one flat list.

    A plain ``gh api`` GET returns only the first page — GitHub's default
    page size silently caps the result, so a comment older than the most
    recent page goes unseen and the find-then-update dedup re-posts a
    duplicate. ``--paginate`` follows the ``Link`` header to the last page;
    ``--slurp`` wraps each page's JSON array into one outer array
    (``[[page1…], [page2…]]``), which this flattens into a single list.

    Non-list pages (a single-object body, an error payload) are skipped so
    a malformed page can never raise. Returns ``[]`` when the outer payload
    is not an array.
    """
    result = _run_gh(
        "gh",
        "api",
        endpoint,
        "--paginate",
        "--slurp",
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list):
        if strict:
            message = "GitHub returned a non-list page set"
            raise ValueError(message)
        return []
    flattened: list[RawAPIDict] = []
    for page in pages:
        if isinstance(page, list):
            if strict and not all(isinstance(item, dict) for item in page):
                message = "GitHub returned a non-object hook"
                raise ValueError(message)
            flattened.extend(cast("list[RawAPIDict]", page))
        elif strict:
            message = "GitHub returned a non-list page"
            raise ValueError(message)
    return flattened


def gh_api_search_paginated(endpoint: str, *, token: str = "") -> list[RawAPIDict]:
    """Fetch every page of a GitHub search endpoint and return a flat item list.

    Search responses wrap results in ``{"items": [...], "total_count": N}``
    rather than a bare JSON array, so ``gh_api_get_paginated`` (which expects
    bare arrays per page via ``--slurp``) cannot be used here.
    ``--paginate`` + ``--slurp`` emits each page as a search-object element;
    this pulls the ``items`` list from each page and flattens them.
    """
    result = _run_gh(
        "gh",
        "api",
        endpoint,
        "--paginate",
        "--slurp",
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list):
        return []
    items: list[RawAPIDict] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_items = cast("RawAPIDict", page).get("items")
        if isinstance(page_items, list):
            items.extend(cast("list[RawAPIDict]", page_items))
    return items


def _gh_api_write(endpoint: str, payload: RawAPIDict, *, method: str, token: str = "") -> object:
    """Send a body-carrying API write with a request-scoped token and bounded timeout."""
    if not token:
        raise CommandFailedError(
            ["gh", "api", endpoint],
            4,
            "",
            "github_token_pass_key resolved no token; refusing ambient gh authentication",
        )
    result = _run_gh(
        *[
            "gh",
            "api",
            endpoint,
            "--method",
            method,
            "--header",
            "Accept: application/vnd.github+json",
            "--input",
            "-",
        ],
        stdin_text=json.dumps(payload),
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def _gh_api_post(endpoint: str, payload: RawAPIDict, *, token: str = "") -> object:
    """Call ``gh api`` (POST) and return parsed JSON."""
    return _gh_api_write(endpoint, payload, method="POST", token=token)


def _gh_api_patch(endpoint: str, payload: RawAPIDict, *, token: str = "") -> object:
    """Call ``gh api`` (PATCH) and return parsed JSON."""
    return _gh_api_write(endpoint, payload, method="PATCH", token=token)


def _gh_api_empty(endpoint: str, *, method: str, token: str = "") -> None:
    _run_gh(
        "gh",
        "api",
        endpoint,
        "--method",
        method,
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )


def _gh_api_delete(endpoint: str, *, token: str = "") -> None:
    _gh_api_empty(endpoint, method="DELETE", token=token)


def parse_issue_ref(issue_url: str) -> tuple[str, int] | None:
    """The ``(owner/repo, number)`` of a GitHub issue URL, or ``None`` when unparsable.

    The single source of truth for the ``urlparse`` + ``_ISSUE_URL_RE`` preamble the
    issue methods (close/update/get/comment) otherwise each duplicate.
    """
    match = _ISSUE_URL_RE.match(urlparse(issue_url).path)
    if match is None:
        return None
    return f"{match['owner']}/{match['repo']}", int(match["number"])
