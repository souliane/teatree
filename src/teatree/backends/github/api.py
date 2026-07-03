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
from teatree.utils.run import CompletedProcess, run_allowed_to_fail, run_checked

_ISSUE_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")


def _run_gh(*args: str, token: str = "") -> CompletedProcess[str]:
    """Run a ``gh`` CLI command and return the result.

    Auth via ``GH_TOKEN`` env, never ``--header``: only ``gh api`` accepts
    ``--header``; injecting it into ``gh pr create`` fails with
    ``unknown flag --header``.
    """
    env = {**os.environ, "GH_TOKEN": token} if token else None
    return run_checked(list(args), env=env)


def gh_ambient_auth_available() -> bool:
    """Whether ``gh``'s own logged-in account (no explicit token) is usable.

    ``gh auth status`` exits 0 when the CLI has a valid active account.
    :func:`teatree.backends.loader.get_code_host_for_repo` uses this to fail
    fast with a clear message instead of letting a raw ``gh`` auth error
    surface deep inside a PR-creation call.
    """
    try:
        result = run_allowed_to_fail(["gh", "auth", "status"], expected_codes=None)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _gh_api_get(endpoint: str, *, token: str = "") -> object:
    """Call ``gh api`` (GET) and return parsed JSON."""
    result = _run_gh(
        "gh",
        "api",
        endpoint,
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
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


def _gh_api_post(endpoint: str, payload: RawAPIDict, *, token: str = "") -> object:
    """Call ``gh api`` (POST) and return parsed JSON."""
    cmd = [
        "gh",
        "api",
        endpoint,
        "--method",
        "POST",
        "--header",
        "Accept: application/vnd.github+json",
        "--input",
        "-",
    ]
    if token:
        cmd.extend(["--header", f"Authorization: Bearer {token}"])
    result = run_checked(cmd, stdin_text=json.dumps(payload))
    return json.loads(result.stdout)


def _gh_api_patch(endpoint: str, payload: RawAPIDict, *, token: str = "") -> object:
    """Call ``gh api`` (PATCH) and return parsed JSON."""
    cmd = [
        "gh",
        "api",
        endpoint,
        "--method",
        "PATCH",
        "--header",
        "Accept: application/vnd.github+json",
        "--input",
        "-",
    ]
    if token:
        cmd.extend(["--header", f"Authorization: Bearer {token}"])
    result = run_checked(cmd, stdin_text=json.dumps(payload))
    return json.loads(result.stdout)


def _parse_issue_ref(issue_url: str) -> tuple[str, int] | None:
    """The ``(owner/repo, number)`` of a GitHub issue URL, or ``None`` when unparsable.

    The single source of truth for the ``urlparse`` + ``_ISSUE_URL_RE`` preamble the
    issue methods (close/update/get/comment) otherwise each duplicate.
    """
    match = _ISSUE_URL_RE.match(urlparse(issue_url).path)
    if match is None:
        return None
    return f"{match['owner']}/{match['repo']}", int(match["number"])
