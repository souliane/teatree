"""Audit mini-loop — per-overlay failed-E2E posts.

The legacy global outbound audit scanner stays in the always-on
``dispatch`` mini-loop because it has no graceful-degradation path; the
per-overlay failed-E2E verifier lives here.
"""

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
        jobs.extend(jobs_for_domain(Domain.AUDIT, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="audit",
    default_cadence_seconds=600,
    build_jobs=_build_jobs,
)
