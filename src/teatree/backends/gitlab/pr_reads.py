"""GitLab wave-2 read helpers — PR list/diff/commits + repo metadata (#3076).

Free functions the :class:`~teatree.backends.gitlab.GitLabCodeHost` delegates the
wave-2 ``CodeHostBackend`` reads to, keeping the host class focused on the
Protocol surface and under the module-health LOC cap — the same split shape as
:mod:`teatree.backends.gitlab.uploads` / :mod:`teatree.backends.gitlab.subissues`.
An unresolvable project degrades to an empty list (list reads) or a structured
``{"error": ...}`` (``repo_metadata``) so an unknown repo never crashes the caller.
"""

import logging
from collections.abc import Callable
from urllib.parse import quote_plus, urlencode

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.types import RawAPIDict
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)


class ProjectUnresolvedError(RuntimeError):
    """A read that must not degrade to ``[]`` could not resolve its GitLab project."""


def state_filter(state: str) -> str:
    """Map the cross-host ``open`` qualifier to GitLab's ``opened`` list filter.

    GitLab's ``merge_requests?state=`` accepts ``opened`` / ``closed`` / ``merged``
    / ``locked`` / ``all``; the cross-host tools use GitHub's ``open`` spelling, so
    only that one word needs translating. Any other value passes through verbatim.
    """
    return "opened" if state == "open" else state


def list_project_prs(client: GitLabAPI, project: ProjectInfo | None, *, state: str, author: str) -> list[RawAPIDict]:
    if project is None:
        return []
    params = ["per_page=100"]
    if state:
        params.append(f"state={state_filter(state)}")
    if author:
        params.append(f"author_username={quote_plus(author)}")
    return client.get_json_paginated(f"projects/{project.project_id}/merge_requests?{'&'.join(params)}")


def list_project_merged_prs_since(
    client: GitLabAPI,
    project: ProjectInfo | None,
    *,
    repo: str,
    since: str,
) -> list[RawAPIDict]:
    """MRs on *project* merged at or after ISO-8601 *since*, newest page size first.

    Unlike its sibling reads this RAISES on an unresolvable project instead of
    degrading to ``[]``: downstream an empty list means "the factory shipped
    nothing", so a failed lookup returned as empty would manufacture that alarm.
    ``updated_after`` is the only server-side time filter GitLab offers here, and an
    MR can be touched after its merge, so ``merged_at`` is re-checked client-side.
    """
    if project is None:
        msg = f"could not resolve GitLab project for {repo!r}; the merged-MR read is indeterminate, not empty"
        raise ProjectUnresolvedError(msg)
    params = f"state=merged&updated_after={quote_plus(since)}&per_page=100"
    page = client.get_json_paginated(f"projects/{project.project_id}/merge_requests?{params}")
    return [item for item in page if str(item.get("merged_at") or "") >= since]


def project_pr_diff(client: GitLabAPI, project: ProjectInfo | None, *, pr_iid: int) -> list[RawAPIDict]:
    if project is None:
        return []
    return client.get_json_paginated(f"projects/{project.project_id}/merge_requests/{pr_iid}/diffs?per_page=100")


def list_project_pr_commits(client: GitLabAPI, project: ProjectInfo | None, *, pr_iid: int) -> list[RawAPIDict]:
    if project is None:
        return []
    return client.get_json_paginated(f"projects/{project.project_id}/merge_requests/{pr_iid}/commits?per_page=100")


def repo_metadata(project: ProjectInfo | None, *, repo: str) -> RawAPIDict:
    if project is None:
        return {"error": f"Could not resolve project: {repo}"}
    return {
        "id": project.project_id,
        "path_with_namespace": project.path_with_namespace,
        "short_name": project.short_name,
        "default_branch": project.default_branch,
    }


def open_mr_url_for_branch(
    client: GitLabAPI,
    resolve_project: Callable[[str], ProjectInfo | None],
    *,
    repo: str,
    branch: str,
) -> str | None:
    """The OPEN MR whose source is *branch*: the url, ``""`` for none, ``None`` for unknown.

    Read over HTTP rather than a forge CLI because the deploy image deliberately ships
    none for GitLab, so a CLI probe answered UNKNOWN for every repo inside the container
    and ``pr ensure-pr`` reported a permanent ``owed`` with no merge request created.
    Needs only the GitLab token, which that image already has.

    Fails CLOSED at every step: an empty *branch* asks nothing (an unfiltered list would
    answer with some OTHER branch's MR), an unresolvable project is UNKNOWN, and any
    transport error is UNKNOWN — never verified absence.
    """
    if not branch:
        return None
    try:
        project = resolve_project(repo)
        if project is None:
            return None
        return _first_open_mr_url(client, project, branch=branch)
    except Exception as exc:  # noqa: BLE001 — fail closed: an unread probe must never read as absence.
        warn_throttled(
            logger,
            f"gitlab-open-mr-probe:{repo}:{branch}",
            "GitLab open-MR probe failed for %s on %s — reporting UNKNOWN: %s",
            repo,
            branch,
            exc,
        )
        return None


def _first_open_mr_url(client: GitLabAPI, project: ProjectInfo, *, branch: str) -> str | None:
    """The first OPEN MR row's ``web_url``; ``""`` when there are none, ``None`` when unreadable.

    The request names an explicit field selector, so a row missing ``web_url`` is a
    changed output schema, never an MR with no url: reporting it as found-with-``""``
    let a fail-closed caller read an unverified open MR as verified absence (#4116).
    """
    query = urlencode({"state": "opened", "source_branch": branch, "per_page": 1})
    rows = client.get_json(f"projects/{project.project_id}/merge_requests?{query}")
    if not isinstance(rows, list):
        return None
    if not rows:
        return ""
    url = rows[0].get("web_url") if isinstance(rows[0], dict) else None
    if not isinstance(url, str) or not url:
        logger.warning("GitLab open-MR probe returned a row with no web_url for %s — UNKNOWN", branch)
        return None
    return url
