"""Issue-level forge reads — the two verdicts the board reconcile asks about an issue.

Both resolve the per-URL code host with the owning overlay's credentials and read the
one ``CodeHostBackend.get_issue`` seam, so a caller never branches on platform. They
fail in OPPOSITE directions on purpose, because their consumers act on opposite
answers: an uncertain "is it done?" must not advance a ticket, and an uncertain "was
it reopened?" must not revive one.

Split out of ``backends.loader`` so that module stays about RESOLVING a backend; these
two are reads performed THROUGH one.
"""

import logging
from typing import TYPE_CHECKING

from teatree.backends.issue_reopen import reopen_state_from_payload
from teatree.backends.loader import get_code_host_for_url
from teatree.core.backend_protocols import IssueReopenState

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


def issue_is_done(overlay: "OverlayBase", issue_url: str) -> bool:
    """Whether *issue_url*'s upstream issue is done per *overlay*'s ``is_issue_done``.

    The single completion-detection seam the ``sync-completions`` sweep and the
    ``TicketCompletionScanner`` both consult before advancing a post-ship ticket.
    Fail-SKIP: an unresolvable host, a fetch failure, an error payload, or a
    non-dict response all return ``False`` (never advance on uncertainty) and a
    fetch failure is logged, never raised — it must not abort the sweep or wedge
    the scan.
    """
    host = get_code_host_for_url(overlay, issue_url)
    if host is None:
        return False
    try:
        issue_data = host.get_issue(issue_url)
    except Exception:  # noqa: BLE001 — a fetch failure skips the ticket, never aborts the caller
        logger.warning("Failed to fetch issue %s — skipping completion check", issue_url)
        return False
    if not isinstance(issue_data, dict) or "error" in issue_data:
        return False
    return bool(overlay.is_issue_done(issue_data))


def issue_reopen_state(overlay: "OverlayBase", issue_url: str) -> IssueReopenState:
    """Whether *issue_url* was closed and then REOPENED, via that same ``get_issue`` seam.

    Fail-CLOSED: an unresolvable host, a fetch failure, or any payload
    :func:`~teatree.backends.issue_reopen.reopen_state_from_payload` cannot classify all
    return ``UNKNOWN``. The board reconcile revives a delivered ticket on this verdict,
    so an unreachable forge must never read as "reopened" (#4152).
    """
    host = get_code_host_for_url(overlay, issue_url)
    if host is None:
        return IssueReopenState.UNKNOWN
    try:
        issue_data = host.get_issue(issue_url)
    except Exception:  # noqa: BLE001 — a fetch failure is UNKNOWN, never an abort of the sweep
        logger.warning("Failed to fetch issue %s — skipping reopen check", issue_url)
        return IssueReopenState.UNKNOWN
    return reopen_state_from_payload(issue_data)
