"""Closed-issue dispatch gate: no implementing agent is spawned on an issue already closed (#2663).

The tick's ``TicketDispositionScanner`` sees a closed issue and drives the
mechanical auto-ignore, but only on its own cadence and only for the states it
walks. That leaves a window a claimed ``Task`` runs inside: the issue closed
after the task was queued, the ticket has not been ignored yet, and the coder
implements work the owner already decided against. Measured, that window cost
ten agent cycles across nine tickets and ~800 uncommitted lines that could never
be landed, because the honest thing for each of those runs to do was stop.

This is the seam's sibling to
:mod:`~teatree.core.gates.plan_dispatch_gate`: the same pre-harness chokepoint,
the same "return a reason rather than raise" contract so the refusal is recorded
as the attempt's own failure text, and the same reuse of ``IMPLEMENTING_PHASES``
so a read-only or coordinating agent is never refused.

It FAILS OPEN. An unresolvable host, a fetch failure, or a payload that cannot be
classified all return ``None`` and the dispatch proceeds — a forge outage must not
freeze the factory, and the disposition scanner retries every tick anyway. That
is the opposite direction from the ``IssueReopenState`` reader, and deliberately
so: there, acting wrongly REVIVES delivered work; here, acting wrongly BLOCKS
live work, so uncertainty resolves toward letting the dispatch through.

``core`` reaches the forge only through the backend-registry seam, never a direct
``teatree.backends`` import — mirroring :mod:`teatree.core.issue_title`. That is
also what lets ``teatree.agents`` consume the gate at all: it declares no
``teatree.backends`` edge either, so the read has to live on this side.
"""

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from teatree.core.gates.plan_dispatch_gate import IMPLEMENTING_PHASES, SUBAGENT_BY_IMPLEMENTING_PHASE
from teatree.core.modelkit.phases import normalize_phase

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

#: Greppable marker leading every refusal, matching the ``plan_missing:`` sibling
#: recorded at the same pre-harness seam. Kept in step with
#: ``FailureKind.ISSUE_CLOSED``'s matcher by the taxonomy test.
ISSUE_CLOSED_PREFIX = "issue_closed: "

#: Forge states that mean the issue is settled. The disposition scanner imports
#: THIS set rather than re-listing it, so the gate refuses exactly what the tick
#: auto-ignores — a scanner that learned a fourth spelling the gate had not would
#: ignore the ticket while still letting its coder run.
CLOSED_ISSUE_STATES: frozenset[str] = frozenset({"closed", "completed", "cancelled"})


class IssueOpenState(StrEnum):
    """Whether a forge issue is still open, per its own payload.

    Three-valued because a ``bool`` cannot say "the forge did not answer", and a
    probe that launders a read failure into ``closed`` would refuse every dispatch
    on the box the moment the forge blinked.
    """

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


def open_state_from_payload(issue_data: object) -> IssueOpenState:
    """Classify a raw forge issue payload as OPEN / CLOSED / UNKNOWN.

    Pure and forge-agnostic: the caller owns the fetch and its failure modes, so
    this never raises. Every shape it cannot positively classify — a non-dict, an
    ``{"error": ...}`` envelope, a missing or non-string ``state`` — is UNKNOWN.
    """
    if not isinstance(issue_data, dict):
        return IssueOpenState.UNKNOWN
    payload = cast("RawAPIDict", issue_data)
    if "error" in payload:
        return IssueOpenState.UNKNOWN
    state = payload.get("state")
    if not isinstance(state, str):
        return IssueOpenState.UNKNOWN
    return IssueOpenState.CLOSED if state.lower() in CLOSED_ISSUE_STATES else IssueOpenState.OPEN


def closed_reason_from_payload(issue_data: object) -> str:
    """The forge's own words for WHY the issue closed, or ``""`` when it says nothing.

    GitHub's ``state_reason`` separates ``not_planned`` (the owner decided against
    it) from ``completed`` (someone already did it). Both stop the dispatch, but
    the operator reading the refusal needs to know which one they are looking at.
    """
    if not isinstance(issue_data, dict):
        return ""
    reason = cast("RawAPIDict", issue_data).get("state_reason")
    return reason if isinstance(reason, str) else ""


def closed_issue_dispatch_refusal(ticket: "Ticket", *, phase: str) -> str | None:
    """The refusal for an implementing dispatch whose issue is already closed, else ``None``.

    ``None`` for every non-implementing phase, every reviewer-role ticket (whose
    ``issue_url`` is a PR someone else owns), every non-forge sentinel URL, and
    every ticket already known to have no remote — so a dispatch is byte-identical
    to today unless the forge positively says the issue is closed.
    """
    from teatree.core.forge_url import is_forge_url  # noqa: PLC0415 — deferred: ORM/app-registry
    from teatree.core.models.ticket import Ticket as TicketModel  # noqa: PLC0415 — deferred: ORM/app-registry

    if normalize_phase(phase) not in IMPLEMENTING_PHASES:
        return None
    if ticket.role == TicketModel.Role.REVIEWER:
        return None
    if not is_forge_url(ticket.issue_url) or ticket.remote_missing:
        return None
    issue_data = _fetch_issue(ticket)
    if open_state_from_payload(issue_data) is not IssueOpenState.CLOSED:
        return None
    return _refusal(ticket, normalize_phase(phase), closed_reason_from_payload(issue_data))


def _fetch_issue(ticket: "Ticket") -> object:
    """*ticket*'s live issue payload, or ``None`` for every failure — the fail-open read."""
    from teatree.core.backend_registry import get_backend_provider  # noqa: PLC0415 — deferred: ORM/app-registry
    from teatree.core.overlay_loader import get_overlay_for_ticket  # noqa: PLC0415 — deferred: ORM/app-registry

    try:
        host = get_backend_provider().get_code_host_for_url(get_overlay_for_ticket(ticket), ticket.issue_url)
        if host is None:
            return None
        return host.get_issue(ticket.issue_url)
    except Exception:  # noqa: BLE001 — a read this gate cannot complete lets the dispatch through, never blocks it
        logger.warning("Could not read issue %s — letting the dispatch through", ticket.issue_url)
        return None


def _refusal(ticket: "Ticket", canonical_phase: str, closed_reason: str) -> str:
    said = f" ({closed_reason})" if closed_reason else ""
    return (
        f"{ISSUE_CLOSED_PREFIX}refusing to dispatch {SUBAGENT_BY_IMPLEMENTING_PHASE[canonical_phase]} for ticket "
        f"{ticket.pk} ({canonical_phase}) — its issue {ticket.issue_url} is CLOSED{said} on the forge, so "
        f"implementing it would deliver work somebody already decided against. Nothing here is lost: the next "
        f"disposition scan auto-ignores the ticket. To act now, reopen the issue if the close was wrong, or "
        f"record the decision with `t3 <overlay> ticket transition {ticket.pk} ignore` (reversible with "
        f"`unignore`). Salvage any work already in the worktree with `t3 <overlay> workspace salvage` first."
    )
