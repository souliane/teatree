"""GitLab sub-issue (work-item) hierarchy helpers — the create_sub_issue concern.

GitLab forbids an Issue→Issue parent link, so a child is created as a plain
issue, converted to its work-item type via ``workItemConvert``, then linked under
the parent via ``workItemUpdate``. These free functions hold that GraphQL
machinery AND the :func:`create_child` orchestration over them, so the host
class stays a thin delegate focused on the cross-host Protocol surface. The
client is injected (like the merge-RPC runner), and so is the plain-issue
``create_issue`` the first hop needs — that keeps the shared leak-scrubbed create
path on the host rather than forking a second one here.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from teatree.backends.gitlab.api import GitLabAPI
from teatree.backends.gitlab.payloads import (
    WORK_ITEM_CONVERT_MUTATION,
    WORK_ITEM_ID_QUERY,
    WORK_ITEM_SET_PARENT_MUTATION,
    WORK_ITEM_TYPE_ID_QUERY,
    mutation_errors,
    work_item_global_id,
    work_item_type_global_id,
)
from teatree.types import RawAPIDict

_ISSUE_OR_WORKITEM_URL_RE = re.compile(r"^/(?P<path>.+?)/-/(?:issues|work_items)/(?P<iid>\d+)/?$")


@dataclass(frozen=True)
class ChildSpec:
    """What the caller wants the child work item to be."""

    parent_url: str
    title: str
    body: str
    labels: list[str] | None = None
    child_type: str = "Task"


@dataclass(frozen=True)
class SubContext:
    repo: str
    project_path: str
    parent_gid: str
    type_gid: str


def work_item_gid(client: GitLabAPI, project_path: str, iid: int) -> str | None:
    data = client.graphql(WORK_ITEM_ID_QUERY, {"projectPath": project_path, "iid": str(iid)})
    return work_item_global_id(data)


def work_item_type_gid(client: GitLabAPI, project_path: str, type_name: str) -> str | None:
    data = client.graphql(WORK_ITEM_TYPE_ID_QUERY, {"projectPath": project_path})
    return work_item_type_global_id(data, type_name)


def run_work_item_mutation(client: GitLabAPI, mutation: str, variables: RawAPIDict, field: str) -> list[str]:
    data = client.graphql(mutation, variables)
    return mutation_errors(data, field)


def resolve_sub_context(client: GitLabAPI, parent_url: str, child_type: str) -> "SubContext | RawAPIDict":
    match = _ISSUE_OR_WORKITEM_URL_RE.match(urlparse(parent_url).path)
    if match is None:
        return {"error": f"Not a GitLab issue URL: {parent_url}"}
    project = client.resolve_project(match["path"])
    if project is None:
        return {"error": f"Could not resolve project: {match['path']}"}
    parent_gid = work_item_gid(client, project.path_with_namespace, int(match["iid"]))
    if parent_gid is None:
        return {"error": f"Could not resolve parent work item: {parent_url}"}
    type_gid = work_item_type_gid(client, project.path_with_namespace, child_type)
    if type_gid is None:
        return {"error": f"Unknown work item type: {child_type}"}
    return SubContext(
        repo=match["path"],
        project_path=project.path_with_namespace,
        parent_gid=parent_gid,
        type_gid=type_gid,
    )


def convert_and_link(client: GitLabAPI, child_gid: str, context: SubContext, child_type: str) -> RawAPIDict | None:
    convert_errors = run_work_item_mutation(
        client,
        WORK_ITEM_CONVERT_MUTATION,
        {"id": child_gid, "typeId": context.type_gid},
        "workItemConvert",
    )
    if convert_errors:
        return {"error": f"Convert to {child_type} failed: {'; '.join(convert_errors)}"}
    link_errors = run_work_item_mutation(
        client,
        WORK_ITEM_SET_PARENT_MUTATION,
        {"id": child_gid, "parentId": context.parent_gid},
        "workItemUpdate",
    )
    if link_errors:
        return {"error": f"Parent link failed: {'; '.join(link_errors)}"}
    return None


def create_child(client: GitLabAPI, create_issue: Callable[..., RawAPIDict], spec: ChildSpec) -> RawAPIDict:
    """Create the child work item *spec* describes, under its parent.

    Three hops, because GitLab forbids an Issue→Issue parent link: create a plain
    issue, convert it to the child type, then nest it. Any failed hop returns
    ``{"error": ...}`` and leaves the partially-created child as a non-linked
    issue — recoverable by hand, which is better than deleting a real issue.
    """
    context = resolve_sub_context(client, spec.parent_url, spec.child_type)
    if not isinstance(context, SubContext):
        return context

    created = create_issue(repo=context.repo, title=spec.title, body=spec.body, labels=spec.labels)
    if "error" in created:
        return created
    child_iid = created.get("iid")
    if not isinstance(child_iid, int):
        return {"error": f"Child issue creation returned no iid: {created}"}

    child_gid = work_item_gid(client, context.project_path, child_iid)
    if child_gid is None:
        return {"error": f"Could not resolve created child work item: {created.get('web_url')}"}

    return convert_and_link(client, child_gid, context, spec.child_type) or created
