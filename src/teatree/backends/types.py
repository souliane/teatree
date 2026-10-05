"""Typed response shapes for backend APIs.

These TypedDicts document the known shapes of responses from backend
protocol implementations.  They do NOT replace the Protocol signatures
(which remain ``dict[str, object]`` for structural typing compatibility).
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import cast


class Service(StrEnum):
    """A third-party service teatree can wrap as an MCP tool group.

    Overlays declare the services they need via
    ``OverlayConfig.required_third_party_services``; the MCP server registers a
    service's tool group only when at least one registered overlay declares it
    (fail-closed — no declaration, no tools). Members exist only for services a
    registered overlay needs wrapped today; add a member when a declarer appears.
    """

    GITHUB = "github"
    GITLAB = "gitlab"
    SLACK = "slack"
    NOTION = "notion"
    SENTRY = "sentry"
    SHAREPOINT = "sharepoint"


def dig(data: object, *keys: str) -> object:
    """Walk nested mapping *keys*, returning ``None`` on any missing/null hop.

    Unlike a chained ``dict.get(k, {})`` this tolerates a key that is present
    but ``null`` — the shape GraphQL returns for an inaccessible user / project
    / work item — where the chained-default form would call ``.get`` on
    ``None`` and crash. Shared by the GitHub and GitLab GraphQL parsers.
    """
    current = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = cast("Mapping[str, object]", current).get(key)
    return current
