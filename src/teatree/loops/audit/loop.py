"""Audit mini-loop — per-overlay failed-E2E posts.

The legacy global outbound audit scanner stays in the always-on
``dispatch`` mini-loop because it has no graceful-degradation path; the
per-overlay failed-E2E verifier lives here.
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
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415
    from teatree.loop.job_identity import Domain  # noqa: PLC0415

    if not backends:
        return []
    all_backends = tuple(backends)
    jobs: list[_ScannerJob] = []
    for backend in backends:
        jobs.extend(jobs_for_domain(Domain.AUDIT, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="audit",
    default_cadence_seconds=600,
    build_jobs=_build_jobs,
)
