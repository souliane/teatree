"""Triage-assessor mini-loop — assess OPEN needs-triage issues behind an ask-gate.

Per-overlay loop, default-OFF behind the ``triage_assessor_enabled`` gate.
Consumes ``Domain.TRIAGE_ASSESSOR`` through the public
:func:`teatree.loop.domain_jobs.jobs_for_domain` seam, so
:func:`teatree.loop.scanner_factories._triage_assessor_scanner_for` stays the
single decision point for whether any scanner is emitted — with the default-OFF
config this mini-loop contributes nothing and the registry/legacy parity stays
byte-for-byte unchanged.

The emitted ``triage_assessor.queued`` signal routes to the shell-denied
``t3:triage-assessor`` agent (via the ``triage_assessing`` pending-task pipeline),
which RETURNS a typed ``triage_recommendations`` envelope. The recorder persists
one ask-gate row per issue plus one ``DeferredQuestion`` — nothing acts
autonomously; the interactive ``t3:triaging-issues`` skill approves/acts.
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
    """Wire each overlay's ``Domain.TRIAGE_ASSESSOR`` slice (empty by default)."""
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415 (lazy import)
    from teatree.loop.job_identity import Domain  # noqa: PLC0415 (lazy import)

    if not backends:
        return []
    all_backends = tuple(backends)
    jobs: list[_ScannerJob] = []
    for backend in backends:
        jobs.extend(jobs_for_domain(Domain.TRIAGE_ASSESSOR, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="triage_assessor",
    default_cadence_seconds=3600,  # 1h tick rate — the scanner self-bounds via its 24h cadence
    build_jobs=_build_jobs,
)
