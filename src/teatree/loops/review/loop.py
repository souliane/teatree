"""Review mini-loop — reviewer-PR / Slack-review-intent / broadcast / codex / sweep wiring.

Five-minute default cadence. Reviewer-PR work is event-bursty (a
colleague pushes a new SHA, the loop fires once, the work is done) so
sub-minute polling buys nothing.
"""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    host: Any | None = None,  # noqa: ANN401 — CodeHostBackend, kept loose
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    """Build per-overlay reviewer-PR + companion review-related scanners."""
    from teatree.loop.scanners import ReviewerPrsScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import Domain, _ScannerJob, jobs_for_domain  # noqa: PLC0415

    if backends:
        all_backends = tuple(backends)
        jobs: list[Any] = []
        for backend in backends:
            jobs.extend(jobs_for_domain(Domain.REVIEW, backend, all_backends=all_backends))
        return jobs
    if host is not None:
        return [_ScannerJob(scanner=ReviewerPrsScanner(host=host), overlay="")]
    return []


MINI_LOOP = MiniLoop(
    name="review",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
)
