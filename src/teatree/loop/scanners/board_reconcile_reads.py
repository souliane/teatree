"""The forge reads of the board janitor's rules: concurrent, and bounded by the scan deadline.

Serial reads took 137 s against the 60 s scan deadline, so the tick abandoned the scanner
before the later rules applied anything. Reads stop :data:`APPLY_MARGIN_SECONDS` before the
deadline so the verdicts already read still have time to be applied.
"""

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from time import monotonic
from typing import TYPE_CHECKING

from teatree.core import overlay_loader
from teatree.loop.scanners.scan_deadline import current_scan_deadline
from teatree.utils.thread_db import close_thread_db_connections

if TYPE_CHECKING:
    from teatree.core.models import Ticket
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

READ_WORKERS = 8
APPLY_MARGIN_SECONDS = 20.0


def read_concurrently[T, R](items: Sequence[T], read: Callable[[T], R]) -> list[tuple[T, R]]:
    """The ``(item, result)`` pairs of *read* over *items*, in input order.

    A read that raises, that the deadline stopped before it started, or that is still in flight
    when the deadline arrives is absent from the result — the call returns then, and the
    straggler finishes on its own thread. Without a scan deadline every item is read.
    """
    deadline = current_scan_deadline()
    stop_at = None if deadline is None else deadline - APPLY_MARGIN_SECONDS
    context = copy_context()

    def read_one(item: T) -> tuple[T, R] | None:
        try:
            if stop_at is not None and monotonic() >= stop_at:
                return None
            return item, read(item)
        except Exception:
            logger.exception("board_reconcile read of %r failed — skipping it", item)
            return None
        finally:
            close_thread_db_connections()

    def read_in_context(item: T) -> tuple[T, R] | None:
        return context.copy().run(read_one, item)

    pool = ThreadPoolExecutor(max_workers=READ_WORKERS)
    try:
        futures = [pool.submit(read_in_context, item) for item in items]
        done = wait(futures, timeout=None if stop_at is None else max(stop_at - monotonic(), 0.0)).done
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    outcomes = [future.result() for future in futures if future in done]
    results = [outcome for outcome in outcomes if outcome is not None]
    if len(results) < len(items):
        logger.warning(
            "board_reconcile read %d of %d item(s); the rest hit the scan deadline or failed", len(results), len(items)
        )
    return results


def _issue_url(ticket: "Ticket") -> str:
    return ticket.issue_url


def read_per_overlay[R](
    tickets: "Sequence[Ticket]",
    read: "Callable[[OverlayBase, str], R]",
    *,
    url_of: "Callable[[Ticket], str]" = _issue_url,
) -> "list[tuple[Ticket, R]]":
    """Read each distinct ``(overlay, URL)`` of *tickets* through its own overlay.

    The URL is the ticket's issue URL unless *url_of* names another, so rows that share one forge issue
    share one read. A ticket whose overlay is not installed here is simply not judged.
    """
    overlays = overlay_loader.get_all_overlays()
    first_per_read: dict[tuple[str, str], Ticket] = {}
    for ticket in tickets:
        if ticket.overlay in overlays:
            first_per_read.setdefault((ticket.overlay, url_of(ticket)), ticket)
    return read_concurrently(
        list(first_per_read.values()), lambda ticket: read(overlays[ticket.overlay], url_of(ticket))
    )
