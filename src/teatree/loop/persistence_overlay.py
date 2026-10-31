"""Which overlay owns a persisted ticket: URL inference first, the scan tag only as the fallback."""

from teatree.core.models import Ticket


def owning_overlay(url: str, scan_tag: str) -> str:
    """Resolve the overlay that *owns* ``url``, preferring URL inference.

    The dispatch payload carries ``overlay`` = the *scanning* overlay's tag
    (``loop/tick.py`` injects ``job.overlay``), not necessarily the overlay
    whose workspace repos own the URL.  When a multi-overlay tick's
    reviewer/orchestrator scanner surfaces an issue/PR owned by a *different*
    overlay, persisting the scan tag leaks the ticket into the scanning
    overlay's statusline zone — and because ``overlay`` is then non-empty,
    ``Ticket.save()`` never runs ``_infer_overlay()`` to correct it (#806,
    incomplete #743).

    Inference (``infer_overlay_for_url``) is the single source of truth; the
    scan tag is only a fallback for the inconclusive case (URL owned by no
    registered overlay, e.g. a host neither overlay declares).
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415 — deferred: loaded at tick time

    return infer_overlay_for_url(url) or scan_tag


def reconcile_existing_overlay(ticket: Ticket, *, created: bool) -> None:
    """Correct an already-persisted ticket whose overlay no longer matches.

    ``get_or_create`` may resolve a pre-existing row whose ``overlay`` was
    written from a stale/wrong scan tag.  Re-infer from ``issue_url`` and
    persist the correction.  ``apply_inferred_overlay`` keeps the #743
    invariant: an inconclusive (empty) inference never blanks a value that
    is already set, so a host no overlay declares is left as-is.
    """
    if created:
        return
    ticket.reconcile_overlay()
