from collections.abc import Mapping
from typing import cast

from teatree.backends.types import dig

WORK_ITEM_STATUS_QUERY = """\
query($projectPath: ID!, $iid: String!) {
    project(fullPath: $projectPath) {
        workItems(iids: [$iid]) {
            nodes {
                widgets {
                    type
                    ... on WorkItemWidgetStatus {
                        status { name }
                    }
                }
            }
        }
    }
}
"""

WORK_ITEM_ID_QUERY = """\
query($projectPath: ID!, $iid: String!) {
    project(fullPath: $projectPath) {
        workItems(iids: [$iid]) {
            nodes { id }
        }
    }
}
"""

WORK_ITEM_TYPE_ID_QUERY = """\
query($projectPath: ID!) {
    workspace: namespace(fullPath: $projectPath) {
        workItemTypes {
            nodes { id name }
        }
    }
}
"""

WORK_ITEM_CONVERT_MUTATION = """\
mutation($id: WorkItemID!, $typeId: WorkItemsTypeID!) {
    workItemConvert(input: {id: $id, workItemTypeId: $typeId}) {
        workItem { id }
        errors
    }
}
"""

WORK_ITEM_SET_PARENT_MUTATION = """\
mutation($id: WorkItemID!, $parentId: WorkItemID!) {
    workItemUpdate(input: {id: $id, hierarchyWidget: {parentId: $parentId}}) {
        workItem { id }
        errors
    }
}
"""


def work_item_global_id(data: object) -> str | None:
    nodes = dig(data, "data", "project", "workItems", "nodes")
    if not isinstance(nodes, list) or not nodes:
        return None
    first_node = nodes[0]
    if not isinstance(first_node, Mapping):
        return None
    gid = cast("Mapping[str, object]", first_node).get("id")
    return gid if isinstance(gid, str) else None


def work_item_type_global_id(data: object, type_name: str) -> str | None:
    nodes = dig(data, "data", "workspace", "workItemTypes", "nodes")
    if not isinstance(nodes, list):
        return None
    wanted = type_name.casefold()
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_dict = cast("Mapping[str, object]", node)
        name = node_dict.get("name")
        gid = node_dict.get("id")
        if isinstance(name, str) and name.casefold() == wanted and isinstance(gid, str):
            return gid
    return None


def mutation_errors(data: object, mutation: str) -> list[str]:
    errors = dig(data, "data", mutation, "errors")
    if isinstance(errors, list):
        return [str(error) for error in errors]
    top_level = dig(data, "errors")
    if isinstance(top_level, list):
        return [str(_error_message(entry)) for entry in top_level]
    return []


def _error_message(entry: object) -> str:
    if isinstance(entry, Mapping):
        message = cast("Mapping[str, object]", entry).get("message")
        if isinstance(message, str):
            return message
    return str(entry)


def status_from_work_item_payload(data: object) -> str | None:
    """Extract the Status-widget name from a work-item GraphQL payload.

    Returns ``None`` for any missing/null hop. GraphQL returns ``null`` (not
    an empty object) for a project the token cannot see / that no longer
    exists, and likewise for an absent ``workItems`` — a chained
    ``.get(..., {})`` does NOT substitute the default when the key is
    present-but-null, so each hop must be isinstance-guarded or ``None.get``
    would crash the whole label sync.
    """
    nodes = dig(data, "data", "project", "workItems", "nodes")
    if not isinstance(nodes, list) or not nodes:
        return None
    first_node = nodes[0]
    if not isinstance(first_node, Mapping):
        return None
    widgets = cast("Mapping[str, object]", first_node).get("widgets", [])
    if not isinstance(widgets, list):
        return None
    for widget in widgets:
        if not isinstance(widget, Mapping):
            continue
        widget_dict = cast("Mapping[str, object]", widget)
        if widget_dict.get("type") == "STATUS":
            status = widget_dict.get("status")
            if isinstance(status, Mapping):
                return str(cast("Mapping[str, object]", status).get("name", ""))
    return None
