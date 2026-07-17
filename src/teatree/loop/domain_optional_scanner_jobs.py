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
    _issue_implementer_scanner_for,
    _pull_main_clone_scanner_for,
    _triage_assessor_scanner_for,
)
from teatree.loop.scanners import Scanner


def _arch_review_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Periodic architectural-review scanner (core platform cadence)."""
    scanner = _architectural_review_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


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
    """Per-overlay pull-main-clone scanner (workspace-repo fast-forward)."""
    scanner = _pull_main_clone_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _issue_implementer_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay issue-implementer scanner behind the default-OFF triple gate (#1553).

    Empty by default — :func:`_issue_implementer_scanner_for` returns
    ``None`` unless the overlay opts in and has in-flight budget — so this
    domain slice contributes nothing to either fan-out path until an overlay
    enables the loop, keeping the registry/legacy parity green.
    """
    scanner = _issue_implementer_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _issue_disposition_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay issue-disposition scanner behind the default-OFF gate (#2122).

    Empty by default — :func:`_issue_disposition_scanner_for` returns ``None``
    unless the overlay opts in (``auto_disposition_enabled``) — so this domain
    slice contributes nothing to either fan-out path until an overlay enables
    the triage scanner, keeping the registry/legacy parity green.
    """
    scanner = _issue_disposition_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _triage_assessor_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay triage-assessor scanner behind the default-OFF gate.

    Empty by default — :func:`_triage_assessor_scanner_for` returns ``None`` unless
    the overlay opts in (``triage_assessor_enabled``) — so this domain slice
    contributes nothing to either fan-out path until an overlay enables the
    assessor, keeping the registry/legacy parity green.
    """
    scanner = _triage_assessor_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]
