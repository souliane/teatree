"""Followup mini-loop — the review-nag cadence.

Issue intake used to live here too; #3634 folded it into the ONE unified
``issue_intake`` scanner so the factory holds a single trust boundary.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(
    *,
    backends: "list[OverlayBackends] | None" = None,
    **_: object,
) -> "list[_ScannerJob]":
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415 — deferred: loaded at tick time, not import
    from teatree.loop.job_identity import Domain, _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time

    if not backends:
        return []
    all_backends = tuple(backends)
    jobs: list[_ScannerJob] = []
    for backend in backends:
        jobs.extend(jobs_for_domain(Domain.FOLLOWUP, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="followup",
    default_cadence_seconds=600,  # 10m — intake is not bursty
    build_jobs=_build_jobs,
)
