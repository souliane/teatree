"""Review mini-loop — the single review intake (self + colleague PRs) + broadcasts.

Five-minute default cadence. Both the owner's OWN open PRs (always) and colleague
requested-reviewer PRs (when ``admit_colleague_prs_to_board`` is on) feed the same
``reviewing`` → ``t3:reviewer`` gate; the author distinction lives upstream in the
intake (#3569). Review work is event-bursty (a new SHA, the loop fires once, the
work is done) so sub-minute polling buys nothing.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(
    *,
    backends: "list[OverlayBackends] | None" = None,
    host: "CodeHostBackend | None" = None,
    **_: object,
) -> "list[_ScannerJob]":
    """Build per-overlay reviewer-PR + companion review-related scanners."""
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415 — deferred: loaded at tick time, not import
    from teatree.loop.job_identity import Domain, _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.loop.scanners import ReviewerPrsScanner  # noqa: PLC0415 — deferred: loaded at tick time, not import

    if backends:
        all_backends = tuple(backends)
        jobs: list[_ScannerJob] = []
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
