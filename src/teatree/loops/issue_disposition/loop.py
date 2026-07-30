"""Issue-disposition mini-loop — auto-close high-confidence DEAD backlog issues.

Per-overlay loop, default-OFF behind the ``auto_disposition_enabled`` gate
(#2122). Consumes ``Domain.ISSUE_DISPOSITION`` through the public
:func:`teatree.loop.domain_jobs.jobs_for_domain` seam, so
:func:`teatree.loop.scanner_factories._issue_disposition_scanner_for` stays the
single decision point for whether any scanner is emitted — with the default-OFF
config this mini-loop contributes nothing and the registry/legacy parity stays
byte-for-byte unchanged (#22).

The emitted ``issue_disposition.close_candidate`` signals route to the
mechanical ``close_dead_issue`` handler — it CLOSES noise with an audit-trail
comment but is physically unable to enqueue work, issuing no ``MergeClear`` and
gaining no merge authority.
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
    """Wire each overlay's ``Domain.ISSUE_DISPOSITION`` slice (empty by default)."""
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415 (lazy import)
    from teatree.loop.job_identity import Domain  # noqa: PLC0415 (lazy import)

    if not backends:
        return []
    all_backends = tuple(backends)
    jobs: list[_ScannerJob] = []
    for backend in backends:
        jobs.extend(jobs_for_domain(Domain.ISSUE_DISPOSITION, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="issue_disposition",
    default_cadence_seconds=300,  # 5m tick rate — the scanner self-bounds via max_closes_per_tick
    build_jobs=_build_jobs,
)
