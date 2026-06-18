"""Docker-by-default routing decision for the metered eval + benchmark lanes.

The metered ``sdk`` lane and ``t3 eval benchmark`` default to running IN the CI
container (``dev/Dockerfile.test``): a metered run bills the API, so it must
never accidentally run on the host. The free / deterministic / subscription
lanes spawn no agent and stay host-default.

Two predicates break the re-route loop and gate the host escape:

*   :func:`in_container` — the docker runner set ``T3_EVAL_IN_CONTAINER=1`` on the
    container, so the in-container re-invocation runs the command in-process.
*   :func:`should_route_to_docker` — a metered command runs in-process ONLY when
    it is already in the container OR ``--local`` was passed; otherwise it routes
    back through :func:`~teatree.cli.eval.docker.run_eval_in_docker`.

:func:`warn_local_metered` prints the loud host-run warning for the ``--local``
escape.
"""

import os
import sys

from teatree.cli.eval.docker import IN_CONTAINER_ENV_VAR


def in_container() -> bool:
    """``True`` when the docker runner's ``T3_EVAL_IN_CONTAINER=1`` marker is set."""
    return bool(os.environ.get(IN_CONTAINER_ENV_VAR))


def should_route_to_docker(*, metered: bool, local: bool) -> bool:
    """Whether a metered/benchmark command must re-route through the CI container.

    Routes to docker only for a *metered* lane that is NOT already in the
    container and was NOT given the explicit ``--local`` host escape. A
    non-metered lane (free / deterministic / subscription) is never routed.
    """
    if not metered:
        return False
    return not (local or in_container())


def warn_local_metered(*, metered: bool) -> None:
    """Print the host-run warning for a ``--local`` metered run (no-op otherwise).

    A ``--local`` metered run is an explicit host escape. Use it for durable
    history gates that must persist/read the runner DB, or for quick host checks;
    Docker remains the reproducible default when no durable ledger is needed.
    """
    if not metered:
        return
    print(  # noqa: T201 — loud, intentional operator warning on stderr.
        "WARNING: --local runs the metered eval on the HOST. Use this only for "
        "durable-history gates that need the runner DB, or for a quick host check; "
        "Docker remains the reproducible default when no durable ledger is needed.",
        file=sys.stderr,
    )
