"""Architectural-review mini-loop — periodic codebase-wide review cadence."""

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
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415
    from teatree.loop.job_identity import Domain  # noqa: PLC0415

    if not backends:
        return []
    all_backends = tuple(backends)
    jobs: list[_ScannerJob] = []
    for backend in backends:
        jobs.extend(jobs_for_domain(Domain.ARCH_REVIEW, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="arch_review",
    default_cadence_seconds=3600,  # 1h tick rate — internal cadence gates daily firing
    build_jobs=_build_jobs,
)
