"""Housekeeping mini-loop — self-update, main clone, override watch, and the undecided gates.

The last two exist because the doctor has no hands: it reports, and nothing converts a report
into work. These turn a standing finding into one deduped question the owner can answer.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(
    *,
    backends: "list[OverlayBackends] | None" = None,
    **_: object,
) -> "list[_ScannerJob]":
    """Wire the global self-update + override-watch jobs and each overlay's main-clone slice.

    The global jobs (``overlay=""``) are about this box rather than any one overlay's tracked
    work — the editable installs themselves, the owner's standing loop overrides, and the
    shipped gates nobody has decided about — so they are built directly here, not via the
    per-overlay seam. The per-overlay pull-main-clone scanner is owned by
    ``Domain.HOUSEKEEPING``.
    """
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415 — deferred: loaded at tick time, not import
    from teatree.loop.global_scanner_factories import _self_update_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import Domain, _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.loop.scanners.ci_oauth_pool_reconcile import (  # noqa: PLC0415 — tick-time import
        CiOauthPoolReconcileScanner,
    )
    from teatree.loop.scanners.inert_gate_questions import InertGateQuestionScanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.scanners.override_lift import OverrideLiftScanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.scanners.stale_control_db_questions import (  # noqa: PLC0415 — tick-time import
        StaleControlDbQuestionScanner,
    )

    jobs: list[_ScannerJob] = [
        _ScannerJob(scanner=OverrideLiftScanner(), overlay=""),
        _ScannerJob(scanner=InertGateQuestionScanner(), overlay=""),
        _ScannerJob(scanner=StaleControlDbQuestionScanner(), overlay=""),
        _ScannerJob(scanner=CiOauthPoolReconcileScanner(), overlay=""),
    ]
    self_update = _self_update_scanner()
    if self_update is not None:
        jobs.append(_ScannerJob(scanner=self_update, overlay=""))
    if backends:
        all_backends = tuple(backends)
        for backend in backends:
            jobs.extend(jobs_for_domain(Domain.HOUSEKEEPING, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="housekeeping",
    default_cadence_seconds=3600,  # 1h — git pulls are not user-visible
    build_jobs=_build_jobs,
    declared_reach=frozenset({LoopReach.INGRESS, LoopReach.COLLEAGUE}),
    determinism=LoopDeterminism.DETERMINISTIC,
)
