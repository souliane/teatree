"""GitLab issue / work-item note operations — the test-plan note concern.

Free functions holding the issue-note CRUD (post / list / update) plus the
shared issue-URL parsing, so :class:`teatree.backends.gitlab.GitLabCodeHost`
delegates with its injected ``GitLabAPI`` client (the same shape as
:mod:`subissues` and :mod:`uploads`), keeping the host class focused on the
cross-host Protocol surface and under the module-health cap.

GitLab exposes the same iid and ``/issues/<iid>/notes`` API under both the
``…/-/issues/<iid>`` and ``…/-/work_items/<iid>`` web URLs, so one regex serves
both. ``repo_for_issue_url`` returns the project slug the note is posted on — the
single source of truth the evidence command uses as the artifact-upload target,
so uploads land in the note's OWN project ``/uploads`` namespace and render.
"""

import re
from urllib.parse import urlparse

from teatree.backends.gitlab.api import GitLabAPI
from teatree.types import RawAPIDict

# ``…/-/issues/<iid>`` and ``…/-/work_items/<iid>`` — GitLab serves the same iid
# and notes API under both; the ``path`` group is the note's project slug.
_ISSUE_OR_WORKITEM_URL_RE = re.compile(r"^/(?P<path>.+?)/-/(?:issues|work_items)/(?P<iid>\d+)/?$")


def repo_for_issue_url(issue_url: str) -> str:
    """Return the project slug that OWNS *issue_url* (the note's own project), or "".

    The evidence command uploads artifacts to this slug so they land in the same
    project's ``/uploads`` namespace the note is created on — a note renders only
    the uploads claimed by its OWN project, so an upload that landed on a
    different repo (e.g. a manifest's second/CI repo) 404s.
    """
    match = _ISSUE_OR_WORKITEM_URL_RE.match(urlparse(issue_url).path)
    return match["path"] if match is not None else ""


def _resolve_issue(client: GitLabAPI, issue_url: str) -> tuple[int, int] | str:
    """Return ``(project_id, iid)`` for *issue_url*, or an error string.

    The error string is the same human message the note methods returned inline,
    so the caller can wrap it in ``{"error": ...}`` (post/update) or treat it as
    "unresolvable" (list).
    """
    match = _ISSUE_OR_WORKITEM_URL_RE.match(urlparse(issue_url).path)
    if match is None:
        return f"Not a GitLab issue URL: {issue_url}"
    project = client.resolve_project(match["path"])
    if project is None:
        return f"Could not resolve project: {match['path']}"
    return project.project_id, int(match["iid"])


def post_issue_comment(client: GitLabAPI, *, issue_url: str, body: str) -> RawAPIDict:
    """Post a note on a GitLab issue / work item, or return ``{"error": ...}``."""
    resolved = _resolve_issue(client, issue_url)
    if isinstance(resolved, str):
        return {"error": resolved}
    project_id, iid = resolved
    return client.post_json(f"projects/{project_id}/issues/{iid}/notes", {"body": body}) or {}


def list_issue_comments(client: GitLabAPI, *, issue_url: str) -> list[RawAPIDict]:
    """List the notes on a GitLab issue / work item, or ``[]`` when unresolvable.

    The caller treats "no comments" and "unresolvable" identically (it creates a
    fresh comment either way).
    """
    resolved = _resolve_issue(client, issue_url)
    if isinstance(resolved, str):
        return []
    project_id, iid = resolved
    return client.get_json_paginated(f"projects/{project_id}/issues/{iid}/notes?per_page=100")


def update_issue_comment(client: GitLabAPI, *, issue_url: str, comment_id: int, body: str) -> RawAPIDict:
    """Edit a note in place on a GitLab issue / work item, or return ``{"error": ...}``.

    Used by ``e2e post-test-plan`` to keep ONE test-plan note per ticket rather
    than appending a new one on re-run.
    """
    resolved = _resolve_issue(client, issue_url)
    if isinstance(resolved, str):
        return {"error": resolved}
    project_id, iid = resolved
    return client.put_json(f"projects/{project_id}/issues/{iid}/notes/{comment_id}", {"body": body}) or {}
