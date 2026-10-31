"""The forge reads of the board janitor's rules: concurrent, and bounded by the scan deadline.

Serial reads took 137 s against the 60 s scan deadline, so the tick abandoned the scanner
before the later rules applied anything. Reads stop :data:`APPLY_MARGIN_SECONDS` before the
deadline so the verdicts already read still have time to be applied.
"""

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
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

    A read that raises, or that the deadline stopped before it started, is absent from the
    result. Without a scan deadline every item is read.
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

    with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
        outcomes = list(pool.map(lambda item: context.copy().run(read_one, item), items))
    results = [outcome for outcome in outcomes if outcome is not None]
    if len(results) < len(items):
        logger.warning(
            "board_reconcile read %d of %d item(s); the rest hit the scan deadline or failed", len(results), len(items)
        )
    return results


def read_per_overlay[R](
    tickets: "Sequence[Ticket]", read: "Callable[[OverlayBase, str], R]"
) -> "list[tuple[Ticket, R]]":
    """Read each distinct ``(overlay, issue URL)`` of *tickets* through its own overlay.

    A ticket whose overlay is not installed here is simply not judged.
    """
    overlays = overlay_loader.get_all_overlays()
    first_per_read: dict[tuple[str, str], Ticket] = {}
    for ticket in tickets:
        if ticket.overlay in overlays:
            first_per_read.setdefault((ticket.overlay, ticket.issue_url), ticket)
    return read_concurrently(
        list(first_per_read.values()), lambda ticket: read(overlays[ticket.overlay], ticket.issue_url)
    )
