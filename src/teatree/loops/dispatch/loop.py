"""Dispatch mini-loop definition — always-on global scanners."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(**_: Any) -> list[Any]:  # noqa: ANN401 — orchestrator passes extra context as open kwargs
    """Build the always-on global scanner triad.

    Delegates to the ``Domain.DISPATCH`` slice of the public
    :func:`teatree.loop.tick_jobs.jobs_for_domain` seam so the legacy
    fan-out stays the single source of which scanners run in this
    mini-loop. The triad carries no per-overlay state.
    """
    from teatree.loop.tick_jobs import Domain, jobs_for_domain  # noqa: PLC0415

    return jobs_for_domain(Domain.DISPATCH)


MINI_LOOP = MiniLoop(
    name="dispatch",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
    always_on=True,
)
