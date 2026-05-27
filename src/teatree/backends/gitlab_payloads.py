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
