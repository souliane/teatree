"""GitHub's read interface: the PR, review, comment, check and issue reads, and no write.

tach ``[[interfaces]]`` limits direct name imports to ``__all__``;
``tests/conformance/test_forge_read_interface.py`` fences module bindings.
Writes stay on :class:`teatree.backends.github.client.GitHubCodeHost`.
"""

from typing import cast
from urllib.parse import quote_plus

from teatree.backends.errors import IssueNotFoundError
from teatree.backends.forge_merge_rpc import fetch_required_checks_rollup as _fetch_required_checks_rollup
from teatree.backends.forge_merge_rpc import fetch_workflow_runs_at_head as _fetch_workflow_runs_at_head
from teatree.backends.github.api import (
    _gh_api_get,
    _gh_api_get_paginated,
    _gh_api_search_paginated,
    _parse_issue_ref,
    is_not_found,
)
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError

__all__ = [
    "fetch_required_checks_rollup",
    "fetch_workflow_runs_at_head",
    "get_issue",
    "list_issue_comments",
    "list_pr_comments",
    "list_pr_commits",
    "list_pr_reviews",
    "list_prs",
    "list_repo_open_issues",
]


def list_prs(*, repo: str, state: str = "", author: str = "", token: str) -> list[RawAPIDict]:
    """Return PRs on ``owner/repo`` filtered by *state* and *author*.

    *state* is GitHub's search qualifier (``open`` / ``closed`` / ``merged``);
    an empty *state* lists every state. Uses the issue-search API so an
    ``author`` filter is a first-class qualifier (the ``pulls`` list endpoint
    cannot filter by author).
    """
    terms = [f"repo:{repo} is:pr"]
    if state:
        terms.append(f"is:{state}")
    if author:
        terms.append(f"author:{author}")
    query = quote_plus(" ".join(terms))
    return _gh_api_search_paginated(f"search/issues?q={query}&per_page=100", token=token)


def list_pr_commits(*, repo: str, pr_iid: int, token: str) -> list[RawAPIDict]:
    """Return the commits on the PR; ``[]`` ONLY for a genuine HTTP 404.

    An unknown PR degrades to ``[]`` but any other failure
    (auth/rate-limit/network/5xx) re-raises rather than being read as an empty
    commit list.
    """
    try:
        return _gh_api_get_paginated(f"repos/{repo}/pulls/{pr_iid}/commits?per_page=100", token=token)
    except CommandFailedError as exc:
        if is_not_found(exc):
            return []
        raise


def list_pr_reviews(*, repo: str, pr_iid: int, token: str) -> list[RawAPIDict]:
    """Return the PR's submitted reviews (state + author); ``[]`` ONLY for a genuine HTTP 404.

    The existing-review surface ``t3 review run`` reports: which logins
    approved, and how many of the viewer's own reviews are still ``PENDING``
    (GitHub's analogue of a GitLab draft note — the endpoint exposes a
    pending review only to the account that owns it). An unknown PR degrades
    to ``[]``, every other failure re-raises rather than reading as "nobody
    has reviewed".
    """
    try:
        return _gh_api_get_paginated(f"repos/{repo}/pulls/{pr_iid}/reviews?per_page=100", token=token)
    except CommandFailedError as exc:
        if is_not_found(exc):
            return []
        raise


def list_pr_comments(*, repo: str, pr_iid: int, token: str) -> list[RawAPIDict]:
    # Paginated: a busy PR has >30 comments, and a first page alone hides the note an updater looks up.
    return _gh_api_get_paginated(f"repos/{repo}/issues/{pr_iid}/comments?per_page=100", token=token)


def list_issue_comments(*, issue_url: str, token: str) -> list[RawAPIDict]:
    """List the comments on a GitHub issue; returns ``[]`` on a non-issue URL."""
    ref = _parse_issue_ref(issue_url)
    if ref is None:
        return []
    repo, number = ref
    return _gh_api_get_paginated(f"repos/{repo}/issues/{number}/comments?per_page=100", token=token)


def fetch_required_checks_rollup(*, slug: str, pr_id: int, token: str) -> list[RawAPIDict]:
    return _fetch_required_checks_rollup(slug=slug, pr_id=pr_id, token=token)


def fetch_workflow_runs_at_head(*, slug: str, head_sha: str, token: str) -> list[RawAPIDict]:
    return _fetch_workflow_runs_at_head(slug=slug, head_sha=head_sha, token=token)


def get_issue(*, issue_url: str, token: str) -> RawAPIDict:
    """Fetch a GitHub issue from its full URL.

    Supports ``https://github.com/<owner>/<repo>/issues/<number>``.
    Returns ``{"error": ...}`` when the URL is not a recognised GitHub
    issue URL.

    Raises:
        IssueNotFoundError: when ``gh api`` reports HTTP 404 (issue
            permanently deleted or never existed).  Any other failure
            (5xx, timeout, network error) propagates as the original
            ``CommandFailedError`` so the scanner keeps retrying it.
    """
    ref = _parse_issue_ref(issue_url)
    if ref is None:
        return {"error": f"Not a GitHub issue URL: {issue_url}"}
    repo, number = ref
    try:
        data = _gh_api_get(f"repos/{repo}/issues/{number}", token=token)
    except CommandFailedError as exc:
        if is_not_found(exc):
            raise IssueNotFoundError(issue_url) from exc
        raise
    return cast("RawAPIDict", data) if isinstance(data, dict) else {"error": f"Issue not found: {issue_url}"}


def list_repo_open_issues(*, repo: str, token: str) -> list[RawAPIDict]:
    """Every OPEN issue on ``owner/repo``, all pages — the create-dedupe landscape (#162).

    Uses the repository issues endpoint rather than search: search is
    eventually consistent and its index lags a just-filed issue by up to a
    minute, which is precisely the window a retrying filer would create a
    duplicate in. GitHub returns pull requests from this endpoint too, so
    entries carrying ``pull_request`` are dropped — an MR is never a
    candidate for an issue dedupe.
    """
    raw = _gh_api_get_paginated(f"repos/{repo}/issues?state=open&per_page=100", token=token)
    return [issue for issue in raw if "pull_request" not in issue]
