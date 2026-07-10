"""GitLab wave-2 read helpers — PR list/diff/commits + repo metadata (#3076).

Free functions the :class:`~teatree.backends.gitlab.GitLabCodeHost` delegates the
wave-2 ``CodeHostBackend`` reads to, keeping the host class focused on the
Protocol surface and under the module-health LOC cap — the same split shape as
:mod:`teatree.backends.gitlab.uploads` / :mod:`teatree.backends.gitlab.subissues`.
An unresolvable project degrades to an empty list (list reads) or a structured
``{"error": ...}`` (``repo_metadata``) so an unknown repo never crashes the caller.
"""

from urllib.parse import quote_plus

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.types import RawAPIDict


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
