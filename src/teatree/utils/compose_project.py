"""The compose project of the deployed stack, resolved with ``deploy/t3``'s precedence."""

import os

from teatree.utils.ports import running_in_container

DEFAULT_COMPOSE_PROJECT = "teatree"


def production_compose_project() -> str:
    """The project ``deploy/t3`` dispatches into; inside a container, always the default.

    ``deploy/t3`` forwards no project variable into the container, so one set there is a
    worktree stack's ``COMPOSE_PROJECT_NAME``, never the deployed stack's.
    """
    if running_in_container():
        return DEFAULT_COMPOSE_PROJECT
    return (
        os.environ.get("TEATREE_COMPOSE_PROJECT") or os.environ.get("COMPOSE_PROJECT_NAME") or DEFAULT_COMPOSE_PROJECT
    )
