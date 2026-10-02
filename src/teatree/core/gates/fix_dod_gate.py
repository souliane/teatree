"""Definition-of-Done gate for fix-tickets: a merge needs a validated FixRecord.

The recurrence this forecloses: a ``kind=fix`` ticket is declared done — its PR
merged — while the underlying behaviour was never structurally shown to be
root-cause-fixed, so the same failure recurs and the prior "done" was a
manifestation patch. teatree's definition-of-done operated on
*artifact-existence* (a PR merged), not on *root-cause-resolution*. A remembered
rule ("done = investigation + root cause + properly fixed") did not hold under
context pressure; this module is the deterministic substitute.

The check is a pure function over durable state — no network, no clock:

Scope
    Only ``Ticket.kind == Ticket.Kind.FIX`` is governed. A ``feature`` ticket
    has no manifestation/root-cause distinction to assert, so the gate never
    fires for it. ``kind`` defaults to ``feature``, so the gate is opt-in by
    classifying a ticket as a fix — it can never silently block existing
    feature work.

Satisfying artifact
    A ``ticket.extra['fix_record']`` mapping with every field of
    :class:`~teatree.core.models.types.FixRecord` non-empty: ``root_cause`` (the
    deepest cause, not the symptom), ``evidence`` (why that is the cause),
    ``regression_test`` (a RED-first regression/conformance test reference),
    ``observed_red`` (an attestation the test was seen failing against the
    pre-fix code), and ``recurrence_fingerprint`` (a stable signature so a
    recurrence detector can match a future failure back to this fix). A partial
    record does NOT satisfy the gate — a manifestation patch with no root cause
    is exactly the case to catch.

How that artifact gets written
    The fixing agent returns a ``fix_record`` object in its result envelope;
    :mod:`teatree.agents.fix_record_recorder` validates it against the same
    :func:`~teatree.core.models.types.fix_record_missing_fields` parse this gate
    uses and writes it through ``ticket.merge_extra``. Self-attestation through a
    CLI was rejected for the reason ``repro_waiver`` already gives. An agent that
    returns NO record is a no-op (an overlay that never emits one keeps today's
    behaviour and reaches DELIVERED via the override below); a MALFORMED one is
    refused loudly at attempt-record time rather than dropped.

Escape hatch
    ``ticket.extra['fix_record_override']`` with a non-empty ``reason`` makes
    the gate pass. The explicit, audited exception for a fix the heuristic
    mis-classifies or a genuinely trivial fix — the gate can never hard-trap a
    legitimate ticket. It is the exception, not the route: the envelope above is
    the route.

The gate is invoked from the ``Ticket.mark_delivered()`` transition body — the
single chokepoint a fix funnels through on its way to done — mirroring how
``check_local_e2e_dod`` sits in ``ship()``. On a block it raises
:class:`FixRecordDodError` (a :class:`InvalidTransitionError` subclass) so the
loop's outer atomic rolls the advance back and the FSM stays put.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.types import FIX_RECORD_FIELDS, fix_record_missing_fields

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


class FixRecordDodError(InvalidTransitionError):
    """A fix-ticket merge was refused: no validated FixRecord.

    A subclass of :class:`InvalidTransitionError` (sibling of
    :class:`~teatree.core.gates.dod_gate.DodLocalE2EError`) so the loop's
    ``Task.complete()`` outer atomic rolls the merge advance back and the FSM
    stays put. The message names the override escape hatch so the operator can
    unblock a legitimately-exempt fix without code.
    """


def is_fix(ticket: "Ticket") -> bool:
    """Return True iff the ticket is classified as a fix (the governed kind)."""
    from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return ticket.kind == Ticket.Kind.FIX


def override_reason(ticket: "Ticket") -> str:
    """The recorded escape-hatch reason, or ``""`` when no override is set."""
    override = (ticket.extra or {}).get("fix_record_override") or {}
    return str(override.get("reason", "")).strip()


def missing_fix_record_fields(ticket: "Ticket") -> list[str]:
    """Return the required FixRecord fields that are absent or blank.

    An empty list means a complete record. Delegates to the shared
    :func:`~teatree.core.models.types.fix_record_missing_fields` parse, so the
    stored record and the returned envelope are judged by ONE predicate.
    """
    return fix_record_missing_fields((ticket.extra or {}).get("fix_record"))


def has_valid_fix_record(ticket: "Ticket") -> bool:
    """Return True iff the ticket carries a complete FixRecord."""
    return not missing_fix_record_fields(ticket)


def check_fix_record_dod(ticket: "Ticket") -> None:
    """Refuse the merge transition when a fix-ticket lacks a validated FixRecord.

    Order of short-circuits (cheapest, most-permissive first):

    1. Not a fix-ticket → pass (the gate only governs ``kind=fix``).
    2. A recorded override reason → pass (logged for audit).
    3. A complete FixRecord → pass.
    4. Otherwise → raise :class:`FixRecordDodError`.
    """
    if not is_fix(ticket):
        return
    reason = override_reason(ticket)
    if reason:
        logger.info("FixRecord DoD gate overridden for ticket %s: %s", ticket.pk, reason)
        return
    missing = missing_fix_record_fields(ticket)
    if not missing:
        return
    msg = (
        f"Refusing to merge fix-ticket {ticket} — its Definition of Done requires a "
        f"validated FixRecord and these fields are missing: {', '.join(missing)}. "
        f"A merged manifestation patch with no stated root cause is not done. The way "
        f"through is to RECORD one: the fixing agent returns a `fix_record` object in "
        f"its result envelope carrying all of {', '.join(FIX_RECORD_FIELDS)}, and the "
        f"recorder writes it here. The override is the exception, not the route — for a "
        f"genuinely trivial or mis-classified fix: "
        f"`t3 <overlay> ticket fix-record-override <id> --reason '<why>'`."
    )
    raise FixRecordDodError(msg)


register_gate("fix_record_dod", check_fix_record_dod)
