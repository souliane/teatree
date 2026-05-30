"""Tickets mini-loop — local Ticket DB scanners + per-host disposition/completion."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    from teatree.loop.tick_jobs import Domain, jobs_for_domain  # noqa: PLC0415

    if not backends:
        return []
    all_backends = tuple(backends)
    jobs: list[Any] = []
    for backend in backends:
        jobs.extend(jobs_for_domain(Domain.TICKETS, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="tickets",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
)
