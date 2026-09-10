"""GitLab wave-2 read helpers — PR list/diff/commits + repo metadata (#3076).

Free functions the :class:`~teatree.backends.gitlab.GitLabCodeHost` delegates the
wave-2 ``CodeHostBackend`` reads to, keeping the host class focused on the
Protocol surface and under the module-health LOC cap — the same split shape as
:mod:`teatree.backends.gitlab.uploads` / :mod:`teatree.backends.gitlab.subissues`.
An unresolvable project degrades to an empty list (list reads) or a structured
``{"error": ...}`` (``repo_metadata``) so an unknown repo never crashes the caller.
"""

from urllib.parse import quote_plus

import httpx

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.core.backend_protocols import PrOpenState
from teatree.types import RawAPIDict

_MR_STATE_MAP: dict[str, PrOpenState] = {
    "opened": PrOpenState.OPEN,
    "merged": PrOpenState.MERGED,
    "closed": PrOpenState.CLOSED,
    "locked": PrOpenState.CLOSED,
}


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


def project_pr_open_state(client: GitLabAPI, *, path: str, iid: str) -> PrOpenState:
    """The MR's genuine open/merged/closed/absent state, ``UNKNOWN`` on any doubt (#1074).

    A 404 on the MR of a project that DID resolve is ``ABSENT`` — the iid names no MR
    that ever existed (#4739). An unresolvable project is ``UNKNOWN``, not ABSENT: it is
    the same read failure a bad token produces, and it proves nothing about the iid.
    """
    try:
        project = client.resolve_project(path)
        if project is None:
            return PrOpenState.UNKNOWN
        mr = client.get_json(f"projects/{project.project_id}/merge_requests/{iid}")
    except httpx.HTTPStatusError as exc:
        return PrOpenState.ABSENT if exc.response.status_code == httpx.codes.NOT_FOUND else PrOpenState.UNKNOWN
    except Exception:  # noqa: BLE001 — fail open: any failure must NOT reap a live review.
        return PrOpenState.UNKNOWN
    if not isinstance(mr, dict):
        return PrOpenState.UNKNOWN
    state = mr.get("state")
    if not isinstance(state, str):
        return PrOpenState.UNKNOWN
    return _MR_STATE_MAP.get(state, PrOpenState.UNKNOWN)
