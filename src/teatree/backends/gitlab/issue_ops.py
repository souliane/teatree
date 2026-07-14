"""GitLab issue-lifecycle operations — create / search / fetch / close / update.

The issue concern of :class:`~teatree.backends.gitlab.client.GitLabCodeHost`, split
out of ``client.py`` in the same delegation idiom the module already uses for
:mod:`~teatree.backends.gitlab.pr_reads`, :mod:`~teatree.backends.gitlab.uploads` and
:mod:`~teatree.backends.gitlab.subissues`: the host keeps the ``CodeHostBackend``
Protocol surface and each method delegates its body to a module-level function here.

Every function takes the resolved :class:`ProjectInfo` (or ``None``) from the host, so
project resolution stays the host's job and these stay pure GitLab-API calls. The
uniform failure shape is ``{"error": ...}`` for the write/fetch paths and ``[]`` for
the search path — a caller treats "no matches" and "unresolvable project" identically.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlparse

import httpx

from teatree.backends.errors import IssueNotFoundError
from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.types import RawAPIDict

#: ``/<group>/.../<repo>/-/issues/<iid>`` — the canonical GitLab issue web path.
ISSUE_URL_RE = re.compile(r"^/(?P<path>.+?)/-/issues/(?P<iid>\d+)/?$")


@dataclass(frozen=True, slots=True)
class NewIssue:
    """The fields of an issue to open — the typed payload :func:`create_issue` takes."""

    repo: str
    title: str
    body: str
    labels: list[str] = field(default_factory=list)


def create_issue(client: GitLabAPI, project: ProjectInfo | None, issue: NewIssue) -> RawAPIDict:
    """Open a GitLab issue and return the created payload.

    The returned dict carries ``web_url`` (the clickable issue link) and ``iid``.
    Returns ``{"error": ...}`` when the project cannot resolve.
    """
    if project is None:
        return {"error": f"Could not resolve project: {issue.repo}"}
    payload: RawAPIDict = {"title": issue.title, "description": issue.body}
    if issue.labels:
        payload["labels"] = ",".join(issue.labels)
    return client.post_json(f"projects/{project.project_id}/issues", payload) or {}


def search_open_issues(client: GitLabAPI, project: ProjectInfo | None, *, query: str) -> list[RawAPIDict]:
    """Return open issues on the project whose title/description match *query*.

    Returns an empty list when the project cannot resolve — the caller treats
    "no matches" and "unresolvable" identically.
    """
    if project is None:
        return []
    endpoint = f"projects/{project.project_id}/issues?state=opened&search={quote_plus(query)}&per_page=100"
    return client.get_json_paginated(endpoint)


def _resolve_issue_ref(client: GitLabAPI, issue_url: str) -> tuple[ProjectInfo, int] | RawAPIDict:
    """Parse *issue_url* to its ``(project, iid)``, or the uniform ``{"error": ...}`` shape.

    The one place the three URL-addressed issue operations below resolve their target,
    so an unrecognised URL and an unresolvable project produce byte-identical errors
    across fetch / close / update.
    """
    match = ISSUE_URL_RE.match(urlparse(issue_url).path)
    if match is None:
        return {"error": f"Not a GitLab issue URL: {issue_url}"}
    project = client.resolve_project(match["path"])
    if project is None:
        return {"error": f"Could not resolve project: {match['path']}"}
    return project, int(match["iid"])


def get_issue(client: GitLabAPI, issue_url: str) -> RawAPIDict:
    """Fetch a GitLab issue from its full URL.

    Supports the canonical web format ``https://gitlab.example.com/<group>/<repo>/-/issues/<iid>``.
    Returns ``{"error": ...}`` when the URL is not a recognised GitLab issue URL or when
    the project cannot be resolved.

    Raises:
        IssueNotFoundError: when the GitLab API returns HTTP 404 (issue permanently
            deleted or never existed). Any other HTTP error (5xx) or network failure
            propagates as-is so the scanner keeps retrying it.
    """
    ref = _resolve_issue_ref(client, issue_url)
    if isinstance(ref, dict):
        return ref
    project, iid = ref
    try:
        issue = client.get_issue(project.project_id, iid)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:  # noqa: PLR2004 — HTTP status compared inline; the numeric code is self-documenting
            raise IssueNotFoundError(issue_url) from exc
        raise
    return issue if isinstance(issue, dict) else {"error": f"Issue not found: {issue_url}"}


def close_issue(client: GitLabAPI, issue_url: str) -> RawAPIDict:
    """Close a GitLab issue.

    Idempotent: ``PUT state_event=close`` is a no-op on an already-closed issue.
    Returns ``{"error": ...}`` when the URL is not a recognised GitLab issue URL or
    when the project cannot be resolved. The optional audit-trail note is posted by
    the host BEFORE this call, so this stays a single-purpose state transition.
    """
    ref = _resolve_issue_ref(client, issue_url)
    if isinstance(ref, dict):
        return ref
    project, iid = ref
    return client.put_json(f"projects/{project.project_id}/issues/{iid}", {"state_event": "close"}) or {}


def update_issue(client: GitLabAPI, issue_url: str, *, body: str) -> RawAPIDict:
    """Replace a GitLab issue's description in place.

    Mirrors :meth:`GitHubCodeHost.update_issue`: the dream-promote flow re-fetches the
    description, upserts a gap checkbox keyed on a stable HTML-comment marker, and
    writes the whole description back. Returns ``{"error": ...}`` when the URL is not a
    recognised GitLab issue URL or when the project cannot be resolved.
    """
    ref = _resolve_issue_ref(client, issue_url)
    if isinstance(ref, dict):
        return ref
    project, iid = ref
    return client.put_json(f"projects/{project.project_id}/issues/{iid}", {"description": body}) or {}
