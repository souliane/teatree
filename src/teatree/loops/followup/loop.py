"""Followup mini-loop — assigned-issue intake + review-nag cadence."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    host: Any | None = None,  # noqa: ANN401 — CodeHostBackend, kept loose to avoid backend imports
    ready_labels: tuple[str, ...] = (),
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    from teatree.loop.scanners import AssignedIssuesScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import Domain, _ScannerJob, jobs_for_domain  # noqa: PLC0415

    if backends:
        all_backends = tuple(backends)
        jobs: list[Any] = []
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
