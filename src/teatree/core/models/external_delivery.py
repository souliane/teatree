"""Delivery-ownership lease for hand-dispatched external delivery (#2104).

A unit delivered out-of-band by a hand-dispatched delivery agent (per
``/teatree-batch``) is implemented directly with no planning phase and no
loop-armed reviewer, so the loop must not re-derive that lifecycle work each
tick. The single delivery-ownership predicate is :func:`under_external_delivery`,
true while a TTL'd ``external_delivery`` lease (stamped on ``Ticket.extra`` via
the canonical locked ``merge_extra``) is live.

The lease is claimed at exactly one seam — the ``workspace ticket`` external
entry the delivery agent runs but the loop's own FSM never does
(:func:`mark_external_delivery`) — and self-reaps on TTL so a crashed external
owner cannot wedge the loop (mirroring ``LoopLease``/``Task`` lease release).
The loop consults the predicate at its two scheduling chokepoints
(``execute_provision`` before ``schedule_planning``; the ``pr_sweep``
review-arm), so it generalises the per-head review dedup (``AutoReviewDispatch``)
from "is this PR-head already being reviewed?" to the unit-level "is this unit
already being delivered by someone else?".

These live at module scope (not on ``Ticket``) to keep the model under the
project's module-health LOC cap; semantically they are siblings of
``ticket.schedule_external_review`` and friends.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.models.types import validated_ticket_extra

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import ExternalDeliveryLease

# A hand-dispatched delivery agent's full cycle comfortably fits one window; a
# crashed owner self-reaps after it so the loop's autonomy resumes. One hour,
# mirroring a generous human delivery turn.
LEASE_SECONDS = 3600


def mark_external_delivery(ticket: "Ticket", *, lease_seconds: int = LEASE_SECONDS) -> None:
    """Claim delivery ownership for a hand-dispatched external agent (#2104).

    Written through the ticket's canonical locked ``merge_extra`` so a
    concurrent ``extra`` writer's key survives. ``lease_seconds`` is the TTL.
    """
    now = timezone.now()
    lease: ExternalDeliveryLease = {
        "at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
    }
    ticket.merge_extra(set_keys={"external_delivery": lease})


def under_external_delivery(ticket: "Ticket") -> bool:
    """True iff a live (non-expired) external-delivery lease exists (#2104).

    The loop's own autonomous FSM never stamps the lease, so this is False on
    every loop-driven ticket; an expired or malformed lease is treated as
    absent so a dead external owner cannot wedge the loop.
    """
    lease = validated_ticket_extra(ticket.extra).get("external_delivery")
    if not isinstance(lease, dict):
        return False
    raw = lease.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return expires_at > timezone.now()
