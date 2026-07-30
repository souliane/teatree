"""Issue-implementer mini-loop — discover + claim labelled issues to auto-implement.

Per-overlay loop, default-OFF behind the ``issue_implementer_enabled``
triple gate (#1553). Consumes ``Domain.ISSUE_IMPLEMENTER`` through the
public :func:`teatree.loop.domain_jobs.jobs_for_domain` seam, so the
``_issue_intake_scanner_for`` gate stays the single decision point
for whether any scanner is emitted — with the default-OFF config this
mini-loop contributes nothing and the registry/legacy parity stays
byte-for-byte unchanged.

The emitted ``issue_intake.admitted`` signals route to
``t3:orchestrator`` (maker-side kickoff) via the dispatch
``AGENT_BY_KIND`` table — starting the normal maker pipeline for the
claimed issue, issuing no ``MergeClear`` and gaining no merge authority.
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
    """Wire each overlay's ``Domain.ISSUE_IMPLEMENTER`` slice (empty by default)."""
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415 — deferred: loaded at tick time, not import
    from teatree.loop.job_identity import Domain  # noqa: PLC0415 — deferred: loaded at tick time, not import

    if not backends:
        return []
    all_backends = tuple(backends)
    jobs: list[_ScannerJob] = []
    for backend in backends:
        jobs.extend(jobs_for_domain(Domain.ISSUE_IMPLEMENTER, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="issue_implementer",
    default_cadence_seconds=3600,  # 1h tick rate — matches issue_implementer_cadence_hours default
    build_jobs=_build_jobs,
)
