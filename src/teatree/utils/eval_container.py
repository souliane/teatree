"""Whether this process runs inside the ephemeral eval Docker container.

Foundation-layer home for the ``T3_EVAL_IN_CONTAINER`` marker. Both the
interface-layer ``teatree.cli.eval`` package (which SETS the marker) and the
domain-layer ``teatree.credential_config`` (which READS it to short-circuit
its DB-backed per-account routing — the container's SQLite is never migrated,
so a live query there is a guaranteed crash) need this predicate. Tach's
layered DAG forbids the domain layer importing the interface layer, so the
marker lives here, below both.
"""

import os

#: Set by ``teatree.cli.eval.docker`` on the container's env so the in-container
#: ``t3 eval`` re-invocation runs in-process instead of re-routing to docker.
IN_CONTAINER_ENV_VAR = "T3_EVAL_IN_CONTAINER"


def in_container() -> bool:
    """``True`` when the docker runner's ``T3_EVAL_IN_CONTAINER=1`` marker is set."""
    return bool(os.environ.get(IN_CONTAINER_ENV_VAR))
