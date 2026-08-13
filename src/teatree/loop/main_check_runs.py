"""The ONE ``check-runs`` argv + page-flattener every commit-scoped CI probe shares (#4090 sibling).

GitHub's REST ``commits/{ref}/check-runs`` endpoint pages at 30 by default. Main carried
86 check-runs at one sampled commit (#4090, ``red_set_surface``) — an unpaginated read of
that endpoint silently sees only page 1, so which check names it reports depends on the
order the API happened to return them (five of the seven required contexts were absent
from page 1 at that same sampled commit). ``red_set_surface`` paginated its own read to
fix that; two sibling call sites asking the identical "is *check_name* green on this
commit?" question (``self_update_ci.GhMainCiStatus``, ``pr_sweep_adapters.GhPrApiClient.
main_check_failed``) built the same unpaginated request independently and inherited the
same truncation gap. This module is the one canonical argv builder + page parser so a
fourth call site cannot reintroduce it — each caller keeps its own subprocess/env/token
plumbing (they differ: overlay-scoped token resolution, a plain token string, a shared
``_run_gh`` helper) and only the request shape + parsing is shared.

``--slurp`` is required, not preferred: bare ``--paginate`` emits concatenated JSON
documents ``json.loads`` rejects, and ``gh`` refuses ``--slurp`` together with ``--jq``,
so classification happens in Python over the parsed runs instead of a server-side filter.
"""

import json
from typing import TypedDict, cast


class CheckRun(TypedDict, total=False):
    """One ``check_runs[]`` entry from the GitHub REST API."""

    name: object
    status: object
    conclusion: object


#: GitHub's default page is 30; a busy repo's default-branch commit can carry many more
#: (86 measured on this repo's ``main``, #4090) — every caller requests a larger page
#: explicitly rather than relying on the ceiling, since ``--paginate`` follows every
#: page regardless.
PAGE_SIZE = 100


def check_runs_argv(*, slug: str, ref: str, page_size: int = PAGE_SIZE) -> list[str]:
    """The ``gh api`` args for every check-run on *slug*'s *ref* commit, across ALL pages.

    Excludes the ``gh`` binary itself so a caller with its own resolved path or its own
    argv-prefixing helper (e.g. a shared ``_run_gh``) can prepend it.
    """
    return ["api", "--paginate", "--slurp", f"repos/{slug}/commits/{ref}/check-runs?per_page={page_size}"]


def parse_check_run_pages(out: str) -> list[CheckRun] | None:
    """Flatten a ``--paginate --slurp`` check-runs response, or ``None`` on no evidence.

    ``--slurp`` yields one list of page bodies, each ``{"total_count", "check_runs"}``, so
    the runs are flattened across pages. A payload carrying literally zero check-runs — an
    unparsable body, or every page reporting an empty list — is indeterminate (nothing has
    reported on that commit yet) and must never be read by a caller as "the named check is
    absent, therefore not failing": :data:`None` keeps that distinction explicit instead of
    collapsing to an empty list a caller could silently treat as evidence.
    """
    try:
        pages = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(pages, list):
        return None
    runs = [
        cast("CheckRun", run)
        for page in pages
        if isinstance(page, dict)
        for run in page.get("check_runs", [])
        if isinstance(run, dict)
    ]
    return runs or None


__all__ = ["PAGE_SIZE", "CheckRun", "check_runs_argv", "parse_check_run_pages"]
