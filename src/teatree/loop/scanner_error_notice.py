"""The ONE owner-facing notice for a scanner that could not read what it needed (#1287).

Both callers of a scanner share it: the periodic dispatcher
(:func:`teatree.loop.domain_jobs._run_job`) and the event-driven
:func:`teatree.loop.sweep_on_demand.trigger_sweep_for_verdict`. The on-demand path
used to swallow the same :class:`ScannerError` into a log line under a command that
then reported success — which is how a sweep that had never enumerated a single MR
looked healthy from every surface (#72).

Deduped once per day per ``(scanner, error_class)`` so a sustained failure states
itself once instead of per tick.
"""

import datetime as dt
import logging

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind
from teatree.messaging import notify_with_fallback
from teatree.types import ScannerError

logger = logging.getLogger(__name__)


def notify_scanner_error(*, label: str, exc: ScannerError, overlay: str, skipped: str = "skipped for one tick") -> None:
    """DM the owner that a scanner is degraded — best-effort, never raises."""
    today = dt.datetime.now(dt.UTC).date().isoformat()
    key = f"scanner_error:{exc.scanner}:{exc.error_class.value}:{today}"
    overlay_tag = f" [overlay={overlay}]" if overlay else ""
    text = f":warning: scanner *{exc.scanner}* hit *{exc.error_class.value}*{overlay_tag} — this scanner is {skipped}."
    if exc.detail:
        text = f"{text}\n_{exc.detail}_"
    try:
        notify_with_fallback(text, kind=NotifyKind.INFO, idempotency_key=key, audience=NotifyAudience.OWNER_ESCALATION)
    except Exception:
        logger.exception("Scanner-error notify_with_fallback failed for %s", label)


__all__ = ["notify_scanner_error"]
