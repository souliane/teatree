"""Optional, default-OFF-gated single-scanner per-overlay job builders.

Each builder here shares one shape: call a ``_xxx_scanner_for(backend)``
factory, and return ``[]`` when it opts out (``None``) or a single
``_ScannerJob`` when it opts in. Split out of ``domain_jobs`` to stay under
the module-health LOC cap (#1983) — this is a natural sub-concern of the
per-overlay domain builders registered in ``_PER_OVERLAY_DOMAIN_BUILDERS``.
"""

from teatree.core.backend_factory import OverlayBackends
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanner_factories import (
    _architectural_review_scanner_for,
    _issue_disposition_scanner_for,
    _issue_intake_scanner_for,
    _pull_main_clone_scanner_for,
    _triage_assessor_scanner_for,
)
from teatree.loop.scanners import BoardReconcileScanner, Scanner


def _arch_review_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Periodic architectural-review scanner — core platform cadence, never opted out here."""
    return [_ScannerJob(scanner=_architectural_review_scanner_for(backend), overlay=backend.name)]


def _failed_e2e_scanner_for(backend: OverlayBackends) -> Scanner | None:
    """Build a per-overlay failed-E2E scanner from overlay watchers (#1295 cap E)."""
    from teatree.loop.scanners.failed_e2e_posts import failed_e2e_scanner_for  # noqa: PLC0415 — tick-time import

    return failed_e2e_scanner_for(backend)


def _audit_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Failed-E2E Slack-post scanner driven by overlay watchers (#1295 cap E)."""
    scanner = _failed_e2e_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _housekeeping_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay pull-main-clone scanner + the board reconcile (#3841).

    The board janitor is hosted here rather than on ``followup`` because
    ``followup`` is ``colleague_facing`` and is therefore skipped under an
    away-class mode — the very situation in which merged tickets pile up
    unreconciled. ``housekeeping`` is enabled, non-colleague-facing, and hourly.
    """
    jobs = [_ScannerJob(scanner=BoardReconcileScanner(overlay_name=backend.name), overlay=backend.name)]
    scanner = _pull_main_clone_scanner_for(backend)
    if scanner is not None:
        jobs.append(_ScannerJob(scanner=scanner, overlay=backend.name))
    return jobs


def _issue_implementer_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay issue-implementer scanner behind the default-OFF triple gate (#1553).

    Empty by default — :func:`_issue_intake_scanner_for` returns
    ``None`` unless the overlay opts in and has in-flight budget — so this
    domain slice contributes nothing to either fan-out path until an overlay
    enables the loop, keeping the registry/legacy parity green.
    """
    scanner = _issue_intake_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _issue_disposition_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Issue-disposition scanner scoped to the canonical core overlay (#2122).

    :func:`_issue_disposition_scanner_for` returns ``None`` unless the overlay is
    the canonical core one, keeping other people's backlogs outside this domain
    slice without a configurable admission gate.
    """
    scanner = _issue_disposition_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _triage_assessor_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay triage-assessor scanner, empty for an overlay with no code host."""
    scanner = _triage_assessor_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]
