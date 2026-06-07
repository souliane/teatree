"""Trivial-work plan-skip marker — the lightweight, audited plan-gate carve-out.

Some AUTHOR tickets are trivial mechanical edits (a typo, a one-line constant
bump) where a full planning phase is pure overhead. This marker lets the
operator record, with a MANDATORY reason, that planning is to be skipped for one
ticket — the lightweight sibling of the heavyweight ``plan-bypass``
(``--human-authorize``) path, which stays intact for the no-real-plan-but-needs-
explicit-sign-off case.

The marker is modelled exactly like the #2104 external-delivery lease: a key on
``Ticket.extra`` written through the canonical locked ``merge_extra`` so a
concurrent ``extra`` writer's key survives. Unlike that lease it is durable (no
TTL) — a trivial-work decision does not expire — and it carries a mandatory
``reason`` plus a recorded ``by``/``at`` audit trail; an unreasoned skip is never
allowed (a blank reason raises before any row is written).

The marker is consumed at the same two seams the external-delivery predicate
uses, mirroring that precedent. ``check_plan_artifact`` (the single ``plan()``
gate) accepts the recorded marker as a satisfying signal, so a trivial-marked
ticket advances STARTED → PLANNED with no ``PlanArtifact`` and no
``--human-authorize``. ``execute_provision`` skips ``schedule_planning`` so the
auto-planner is never scheduled for a trivial-marked AUTHOR ticket.

A malformed or empty-reason marker is treated as absent (fail-safe to "planning
still required"), so a garbled row can never silently skip the gate.

These live at module scope (not on ``Ticket``) to keep the model under the
project's module-health LOC cap; semantically they are siblings of
``external_delivery.mark_external_delivery`` and friends.
"""

from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.models.types import validated_ticket_extra

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import TrivialPlanSkip

_MARKER_KEY = "trivial_plan_skip"


def mark_trivial_plan_skip(ticket: "Ticket", *, reason: str, by: str = "operator") -> None:
    """Mark *ticket* as a trivial mechanical edit whose planning is skipped.

    Written through the ticket's canonical locked ``merge_extra`` so a
    concurrent ``extra`` writer's key survives. ``reason`` is mandatory — a
    blank/whitespace-only reason raises ``ValueError`` before any row is
    written, so the skip is always audited. ``by`` records who decided the skip
    (audit trail); a blank ``by`` is also refused.
    """
    cleaned_reason = reason.strip() if reason else ""
    if not cleaned_reason:
        msg = "reason is required and must be non-empty (an unreasoned plan skip is not allowed)"
        raise ValueError(msg)
    cleaned_by = by.strip() if by else ""
    if not cleaned_by:
        msg = "by is required and must be non-empty"
        raise ValueError(msg)

    marker: TrivialPlanSkip = {
        "reason": cleaned_reason,
        "by": cleaned_by,
        "at": timezone.now().isoformat(),
    }
    ticket.merge_extra(set_keys={_MARKER_KEY: marker})


def trivial_plan_skip_reason(ticket: "Ticket") -> str | None:
    """Return the recorded trivial-skip reason, or ``None`` when not marked.

    A malformed marker (non-dict value, or a dict whose ``reason`` is missing or
    blank) is treated as absent — a garbled row never silently skips the gate.
    """
    marker = validated_ticket_extra(ticket.extra).get(_MARKER_KEY)
    if not isinstance(marker, dict):
        return None
    raw = marker.get("reason")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def is_trivial_plan_skip(ticket: "Ticket") -> bool:
    """True iff *ticket* carries a well-formed trivial-skip marker."""
    return trivial_plan_skip_reason(ticket) is not None
