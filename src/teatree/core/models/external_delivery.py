"""Delivery-ownership lease for hand-dispatched external delivery (#2104).

A unit delivered out-of-band by a hand-dispatched delivery agent (per
``/t3:wip``) is implemented directly with no planning phase and no
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
``schedule_external_review`` and friends.
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
    if expires_at.tzinfo is None:
        # A tz-naive value cannot be compared against the aware ``timezone.now()``
        # (``TypeError: can't compare offset-naive and offset-aware``). Our writer
        # never produces one; treat it as malformed -> absent, mirroring the Q,
        # the conservative direction (a dead/garbage lease never wedges the loop).
        return False
    return expires_at > timezone.now()


# ``mark_external_delivery`` is the ONLY writer of ``expires_at`` and always emits
# ``timezone.now().isoformat()`` — a UTC value whose canonical shape is
# ``YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`` (the fractional part is dropped only when
# the microsecond component is exactly zero). Constraining the DB-layer match to
# this exact shape is the parity-correct mirror of the predicate's
# ``datetime.fromisoformat`` + aware-comparison semantics: a digit-leading
# non-ISO value (``"3000-bogus-not-iso"``) that would otherwise sort *above* the
# now-string under a bare lexicographic range, an alpha-leading value, and a
# tz-naive value are all rejected here exactly as the predicate treats them as
# absent. A plain string upper bound cannot do this (a digit-leading garbage
# string sorts inside any all-nines sentinel), so the shape regex replaces it.
_CANONICAL_EXPIRES_REGEX = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?\+00:00$"


def live_external_delivery_q(*, field_prefix: str = "ticket__", now: datetime | None = None) -> "Q":
    """``Q`` selecting rows whose related ticket has a LIVE external-delivery lease.

    The DB-layer mirror of :func:`under_external_delivery`, used by the dispatch
    chokepoint ``loop.phases.orchestrate._dispatchable_filter`` to exclude every
    phase on a hand-delivered ticket from claim/dispatch (#2217). ``field_prefix``
    is the ORM relation path to the ``Ticket`` (``"ticket__"`` for a ``Task``
    query, ``""`` for a ``Ticket`` query), so one source-of-truth builder serves
    both rootings.

    Liveness requires the JSONField ``external_delivery.expires_at`` string to
    (1) match the canonical writer shape ``YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00``
    AND (2) be lexicographically ``> now.isoformat()``. The shape constraint
    mirrors the predicate's ``fromisoformat`` + aware-comparison semantics: a
    well-formed canonical UTC value sorts lexically by time, so the ``__gt`` bound
    is exact, while a digit-leading / alpha-leading / tz-naive malformed value
    fails the shape and is excluded — exactly as :func:`under_external_delivery`
    treats it as absent. The conservative direction is preserved: anything
    ambiguous is NOT live (-> dispatchable), so a dead/garbage lease never wedges
    the loop.
    """
    now_iso = (now or timezone.now()).isoformat()
    key = f"{field_prefix}extra__external_delivery__expires_at"
    return Q(**{f"{key}__regex": _CANONICAL_EXPIRES_REGEX, f"{key}__gt": now_iso})


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
