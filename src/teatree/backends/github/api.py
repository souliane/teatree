"""GitHub ``gh`` CLI request helpers — the low-level transport concern.

Free functions wrapping ``gh`` / ``gh api`` (GET / paginated GET / search /
POST / PATCH) plus the issue-URL parse, so :class:`teatree.backends.github.GitHubCodeHost`
stays focused on the cross-host Protocol surface and under the module-health cap —
the same split shape as :mod:`teatree.backends.gitlab.api`.
"""

import json
import os
import re
from typing import cast
from urllib.parse import urlparse

from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError, CompletedProcess, run_allowed_to_fail, run_checked

_ISSUE_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")

# Bound every ``gh`` subprocess so a stalled TLS handshake or a hung read degrades
# (raises TimeoutExpired → the caller's fail-open) instead of wedging the
# single-threaded loop indefinitely. Generous enough not to false-trip a
# fully-paginated fetch; the GitLab client already bounds each request at 10s.
_FORGE_READ_TIMEOUT_SECONDS = 60.0


def _run_gh(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
    """Run a ``gh`` CLI command and return the result.

    Auth via ``GH_TOKEN`` env, never ``--header``: only ``gh api`` accepts
    ``--header``; injecting it into ``gh pr create`` fails with
    ``unknown flag --header``. *timeout* (seconds) bounds the subprocess; the
    default ``None`` leaves every existing caller unbounded as before.
    """
    env = {**os.environ, "GH_TOKEN": token} if token else None
    return run_checked(list(args), env=env, timeout=timeout)


def gh_ambient_auth_available() -> bool:
    """Whether ``gh``'s own logged-in account (no explicit token) is usable.

    ``gh auth status`` exits 0 when the CLI has a valid active account.
    :func:`teatree.backends.loader.get_code_host_for_repo` uses this to fail
    fast with a clear message instead of letting a raw ``gh`` auth error
    surface deep inside a PR-creation call.
    """
    try:
        result = run_allowed_to_fail(["gh", "auth", "status"], expected_codes=None, timeout=_FORGE_READ_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def gh_can_push(repo_slug: str, *, token: str = "") -> bool | None:
    """Whether the identity behind *token* (ambient ``gh`` when empty) has push access to *repo_slug*.

    Reads ``repos/{slug}`` ``.permissions.push`` for the authenticated identity:
    ``True`` / ``False`` on a definite answer, ``None`` when it cannot be
    determined — no slug, ``gh`` absent, a network/auth error, an unreadable repo
    (404), or an unparsable payload. :func:`teatree.backends.loader.get_code_host_for_repo`
    reads ``None`` as "keep the configured token": a collaborator override must
    be CERTAIN, so a transient probe failure never switches the PR-authoring
    identity. This is the seam that keeps ``gh pr create``'s ``createPullRequest``
    from running under a non-collaborator token when the logged-in ``gh`` account
    is the collaborator (the "must be a collaborator" abort).
    """
    if not repo_slug:
        return None
    try:
        result = _run_gh(
            "gh",
            "api",
            f"repos/{repo_slug}",
            "--jq",
            ".permissions.push",
            token=token,
            timeout=_FORGE_READ_TIMEOUT_SECONDS,
        )
    except (CommandFailedError, FileNotFoundError):
        return None
    answer = result.stdout.strip().lower()
    if answer == "true":
        return True
    if answer == "false":
        return False
    return None


def _gh_api_get(endpoint: str, *, token: str = "") -> object:
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


def _gh_api_get_paginated(endpoint: str, *, token: str = "") -> list[RawAPIDict]:
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
        return []
    flattened: list[RawAPIDict] = []
    for page in pages:
        if isinstance(page, list):
            flattened.extend(cast("list[RawAPIDict]", page))
    return flattened


def _gh_api_search_paginated(endpoint: str, *, token: str = "") -> list[RawAPIDict]:
    """Fetch every page of a GitHub search endpoint and return a flat item list.

    Search responses wrap results in ``{"items": [...], "total_count": N}``
    rather than a bare JSON array, so ``_gh_api_get_paginated`` (which expects
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
    """Call ``gh api`` with a body-carrying verb (POST / PATCH) and return parsed JSON.

    The token is passed via ``GH_TOKEN`` env, never ``--header
    "Authorization: Bearer <token>"`` — an argv header is visible in
    ``/proc/<pid>/cmdline`` and ``ps`` for the lifetime of the subprocess.
    Every write is timeout-bounded so a hung TLS handshake degrades (raises
    ``TimeoutExpired`` → the caller's fail-open) instead of wedging the
    single-threaded loop.
    """
    env = {**os.environ, "GH_TOKEN": token} if token else None
    result = run_checked(
        [
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
        env=env,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def _gh_api_post(endpoint: str, payload: RawAPIDict, *, token: str = "") -> object:
    """Call ``gh api`` (POST) and return parsed JSON."""
    return _gh_api_write(endpoint, payload, method="POST", token=token)


def _gh_api_patch(endpoint: str, payload: RawAPIDict, *, token: str = "") -> object:
    """Call ``gh api`` (PATCH) and return parsed JSON."""
    return _gh_api_write(endpoint, payload, method="PATCH", token=token)


def _parse_issue_ref(issue_url: str) -> tuple[str, int] | None:
    """The ``(owner/repo, number)`` of a GitHub issue URL, or ``None`` when unparsable.

    The single source of truth for the ``urlparse`` + ``_ISSUE_URL_RE`` preamble the
    issue methods (close/update/get/comment) otherwise each duplicate.
    """
    match = _ISSUE_URL_RE.match(urlparse(issue_url).path)
    if match is None:
        return None
    return f"{match['owner']}/{match['repo']}", int(match["number"])
