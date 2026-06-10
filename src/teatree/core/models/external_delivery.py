"""Delivery-ownership lease for hand-dispatched external delivery (#2104).

A unit delivered out-of-band by a hand-dispatched delivery agent (per
``/teatree-batch``) is implemented directly with no planning phase and no
loop-armed reviewer, so the loop must not re-derive that lifecycle work each
tick. The single delivery-ownership predicate is :func:`under_external_delivery`,
true while a TTL'd ``external_delivery`` lease (stamped on ``Ticket.extra`` via
the canonical locked ``merge_extra``) is live.

The lease is claimed at the ``workspace ticket`` external entry the delivery
agent runs but the loop's own FSM never does (:func:`mark_external_delivery`),
and refreshed on each external-owner FSM seam the delivery agent drives
(``ticket plan``, ``ticket transition``) so an *actively*-delivering owner does
not lapse mid-delivery when the work outruns the TTL. A crashed owner stops
touching those seams, so the lease self-reaps on TTL and the loop's autonomy
resumes (mirroring ``LoopLease``/``Task`` lease release).

The loop consults the lease at three scheduling chokepoints. Two are the
phase-specific seams that read the Python predicate
:func:`under_external_delivery` (``execute_provision`` before
``schedule_planning``; the ``pr_sweep`` review-arm). The third is the global
dispatch chokepoint ``loop.phases.orchestrate._dispatchable_filter`` — the single
``Q`` that gates EVERY task dispatch — which excludes tickets under a live lease
at the DB layer via :func:`live_external_delivery_q`, so NO phase on a
hand-delivered ticket is ever claimed (the #2217 gap: once the external owner
hand-advances STARTED -> PLANNED, any non-planning phase was dispatchable despite
a live lease). Together they generalise the per-head review dedup
(``AutoReviewDispatch``) from "is this PR-head already being reviewed?" to the
unit-level "is this unit already being delivered by someone else?".

These live at module scope (not on ``Ticket``) to keep the model under the
project's module-health LOC cap; semantically they are siblings of
``ticket.schedule_external_review`` and friends.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
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


def refresh_external_delivery_if_active(ticket: "Ticket", *, lease_seconds: int = LEASE_SECONDS) -> bool:
    """Re-stamp the lease's TTL iff one is currently LIVE. Returns whether it did (#2217).

    Called on each external-owner FSM seam the delivery agent drives
    (``ticket plan``, ``ticket transition``) so an *actively*-delivering owner's
    lease never lapses mid-delivery when the work outruns ``LEASE_SECONDS``. It
    is a strict refresh, not a claim: it extends an already-live lease and does
    NOTHING when the lease is absent or already expired, so the loop's own FSM
    (which never stamps a lease) cannot accidentally claim a unit, and a crashed
    owner's lapsed lease still self-reaps to let the loop resume.
    """
    if not under_external_delivery(ticket):
        return False
    mark_external_delivery(ticket, lease_seconds=lease_seconds)
    return True


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


# A canonical UTC ISO timestamp our writers produce always begins with a 4-digit
# year, so it sorts strictly below this all-nines sentinel. Bounding the
# lexicographic ``expires_at`` comparison below the sentinel excludes
# alpha-leading garbage ("not-a-date") that would otherwise sort *above* the
# current-time string and be wrongly admitted as "future"; a real future expiry
# stays inside the bound. This keeps the DB-layer ``Q`` in lockstep with the
# Python predicate's malformed-lease handling.
_MAX_ISO_EXPIRES = "9999-12-31T23:59:59.999999+00:00"


def live_external_delivery_q(*, field_prefix: str = "ticket__", now: datetime | None = None) -> "Q":
    """``Q`` selecting rows whose related ticket has a LIVE external-delivery lease.

    The DB-layer mirror of :func:`under_external_delivery`, used by the dispatch
    chokepoint ``loop.phases.orchestrate._dispatchable_filter`` to exclude every
    phase on a hand-delivered ticket from claim/dispatch (#2217). ``field_prefix``
    is the ORM relation path to the ``Ticket`` (``"ticket__"`` for a ``Task``
    query, ``""`` for a ``Ticket`` query), so one source-of-truth builder serves
    both rootings.

    Liveness is a lexicographic bound on the JSONField ``external_delivery
    .expires_at`` string: ``now.isoformat() < expires_at < _MAX_ISO_EXPIRES``.
    Valid because :func:`mark_external_delivery` always writes a fixed-format UTC
    ``timezone.now().isoformat()`` (``+00:00``) string; the upper sentinel
    excludes malformed alpha-leading values that sort above the now-string, so an
    expired / absent / malformed lease is excluded exactly as the predicate
    treats it as absent.
    """
    now_iso = (now or timezone.now()).isoformat()
    key = f"{field_prefix}extra__external_delivery__expires_at"
    return Q(**{f"{key}__gt": now_iso, f"{key}__lt": _MAX_ISO_EXPIRES})


def not_under_external_delivery_q(*, field_prefix: str = "ticket__", now: datetime | None = None) -> "Q":
    """``Q`` selecting rows whose related ticket is NOT under a live lease (#2217).

    The complement of :func:`live_external_delivery_q`, used by the dispatch
    chokepoint to ADMIT every ticket that is not actively hand-delivered. A bare
    ``~live_external_delivery_q()`` is wrong: SQL three-valued logic makes
    ``NOT (NULL > x)`` evaluate to ``NULL`` (falsy), so a ticket with NO lease
    key — ``expires_at`` extracts to SQL ``NULL`` — would be silently dropped
    from the admitted set, halting all loop dispatch. The explicit ``isnull``
    arm rescues the absent-lease (and any-NULL) rows so they remain dispatchable,
    matching :func:`under_external_delivery` returning ``False`` for them.
    """
    key = f"{field_prefix}extra__external_delivery__expires_at"
    return ~live_external_delivery_q(field_prefix=field_prefix, now=now) | Q(**{f"{key}__isnull": True})
