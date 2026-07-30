"""GitHub Projects v2 board reads — the board-sync concern of the GitHub backend.

Split out of ``backends/github.py`` (which had reached the per-file
module-health LOC cap): the Projects v2 GraphQL query, the ``ProjectItem``
row, and the paginated board walk live here, while ``github.py`` keeps the
``CodeHostBackend`` (PRs, issues, comments). ``github`` re-exports
``ProjectItem`` and ``fetch_project_items`` so existing import sites are
unchanged.
"""

import json
import os
from dataclasses import dataclass

from teatree.backends.types import dig
from teatree.types import RawAPIDict
from teatree.utils.run import run_checked

# Bound every ``gh api graphql`` subprocess so a stalled read degrades (raises
# TimeoutExpired) instead of wedging the single-threaded loop.
_GRAPHQL_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ProjectItem:
    """A single item from a GitHub Projects v2 board."""

    issue_number: int
    title: str
    url: str
    status: str
    position: int
    labels: list[str]
    updated_at: str = ""


def _gh_graphql(query: str, *, token: str = "") -> RawAPIDict:
    """Execute a GraphQL query via ``gh api graphql``.

    The token is passed via ``GH_TOKEN`` env, never ``--header "Authorization:
    Bearer <token>"`` — an argv header leaks the credential to
    ``/proc/<pid>/cmdline`` and ``ps``.
    """
    env = {**os.environ, "GH_TOKEN": token} if token else None
    result = run_checked(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        env=env,
        timeout=_GRAPHQL_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


_PROJECT_ITEMS_QUERY = """\
{{
    user(login: "{owner}") {{
        projectV2(number: {project_number}) {{
            items(first: 100{after}) {{
                pageInfo {{ hasNextPage endCursor }}
                nodes {{
                    fieldValueByName(name: "Status") {{
                        ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
                    }}
                    content {{
                        ... on Issue {{
                            number
                            title
                            url
                            updatedAt
                            labels(first: 10) {{ nodes {{ name }} }}
                        }}
                    }}
                }}
            }}
        }}
    }}
}}"""


def fetch_project_items(
    owner: str,
    project_number: int,
    *,
    token: str = "",
) -> list[ProjectItem]:
    """Fetch all items from a GitHub Projects v2 board, preserving board order.

    The ``items`` connection caps each page at 100 nodes, so a board with more
    than 100 items must be walked page by page via the ``pageInfo`` cursor —
    otherwise every item past the first page is silently dropped from the sync.
    """
    items: list[ProjectItem] = []
    position = 0
    after = ""
    while True:
        query = _PROJECT_ITEMS_QUERY.format(owner=owner, project_number=project_number, after=after)
        data = _gh_graphql(query, token=token)
        # ``dig`` null-guards each hop: GraphQL returns ``null`` (not ``{}``) for
        # a user/project the token cannot see, where a chained ``.get(k, {})``
        # would call ``.get`` on ``None`` and crash the board sync.
        raw_items = dig(data, "data", "user", "projectV2", "items", "nodes")
        nodes = raw_items if isinstance(raw_items, list) else []
        for node in nodes:
            if (item := _project_item_from_node(node, position)) is not None:
                items.append(item)
            position += 1
        if dig(data, "data", "user", "projectV2", "items", "pageInfo", "hasNextPage") is not True:
            return items
        end_cursor = dig(data, "data", "user", "projectV2", "items", "pageInfo", "endCursor")
        if not isinstance(end_cursor, str) or not end_cursor:
            return items
        after = f', after: "{end_cursor}"'


def _project_item_from_node(node: object, position: int) -> ProjectItem | None:
    """Build a :class:`ProjectItem` from one board node, or ``None`` to skip.

    Every field read goes through :func:`dig`, which null-guards each hop and
    returns ``object`` — so a draft item (no ``content``) or a node the token
    cannot fully see degrades to a skip rather than crashing the board sync.
    """
    number = dig(node, "content", "number")
    if not isinstance(number, int):
        return None  # draft item or non-issue content
    status_name = dig(node, "fieldValueByName", "name")
    raw_labels = dig(node, "content", "labels", "nodes")
    label_nodes = raw_labels if isinstance(raw_labels, list) else []
    labels = [str(name) for ln in label_nodes if isinstance(name := dig(ln, "name"), str)]
    return ProjectItem(
        issue_number=number,
        title=str(dig(node, "content", "title") or ""),
        url=str(dig(node, "content", "url") or ""),
        status=str(status_name or ""),
        position=position,
        labels=labels,
        updated_at=str(dig(node, "content", "updatedAt") or ""),
    )
