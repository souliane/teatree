"""Followup mini-loop — assigned-issue intake + review-nag cadence."""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.backends.protocols import CodeHostBackend
    from teatree.core.backend_factory import OverlayBackends
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(
    *,
    backends: "list[OverlayBackends] | None" = None,
    host: "CodeHostBackend | None" = None,
    ready_labels: tuple[str, ...] = (),
    **_: object,
) -> "list[_ScannerJob]":
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415
    from teatree.loop.job_identity import Domain, _ScannerJob  # noqa: PLC0415
    from teatree.loop.scanners import AssignedIssuesScanner  # noqa: PLC0415

    if backends:
        all_backends = tuple(backends)
        jobs: list[_ScannerJob] = []
        for backend in backends:
            jobs.extend(jobs_for_domain(Domain.FOLLOWUP, backend, all_backends=all_backends))
        return jobs
    if host is not None:
        return [_ScannerJob(scanner=AssignedIssuesScanner(host=host, ready_labels=ready_labels), overlay="")]
    return []


MINI_LOOP = MiniLoop(
    name="followup",
    default_cadence_seconds=600,  # 10m — intake is not bursty
    build_jobs=_build_jobs,
)
