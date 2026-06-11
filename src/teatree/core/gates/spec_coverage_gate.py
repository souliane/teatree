"""Per-ticket spec-coverage Definition-of-Done gate.

The recurrence this forecloses: a ticket is declared *done* — its PR merged, its
retro written — on a partial subset of its spec, because only some of the
acceptance criteria were backed by a test. The remaining ACs were never proven,
so "done" was an optimistic claim over an unverified subset. A remembered rule
("done = every AC backed by a test, never a subset") does not hold under context
pressure; this module is the deterministic substitute.

The check is a pure function over durable ``ticket.extra`` state — no network, no
clock — mirroring :mod:`teatree.core.gates.fix_dod_gate` and
:mod:`teatree.core.gates.review_context_gate`.

Opt-in default
    ``require_spec_coverage`` is ``False`` unless configured (per-overlay or
    global ``[teatree]``). With it unset the gate is a NO-OP — existing feature
    delivery is unchanged. A project opts in by setting it ``true``.

Satisfying artifact
    ``ticket.extra['spec_coverage']`` — a mapping with an
    ``acceptance_criteria`` list, each entry an AC mapping with an ``id`` (or a
    ``description`` fallback) and a ``tests`` list naming at least one backing
    test reference. An AC with an empty/absent ``tests`` list is *uncovered*.
    When the gate is on, the manifest is REQUIRED: a ticket carrying no manifest
    (zero ACs proven) is itself a block — that empty subset is exactly the
    "partial subset" the gate exists to refuse.

Escape hatch
    ``ticket.extra['spec_coverage_override']`` with a non-empty ``reason`` makes
    the gate pass (logged for audit) — for a genuinely AC-less ticket (a pure
    refactor, a docs-only change) the heuristic should not hard-trap.

The gate is invoked from the ``Ticket.mark_delivered()`` transition body — the
single chokepoint a ticket funnels through on its way from RETROSPECTED to
DELIVERED, beside ``check_fix_record_dod``. On a block it raises
:class:`SpecCoverageDodError` (an :class:`InvalidTransitionError` subclass) so the
loop's outer atomic rolls the advance back and the ticket stays RETROSPECTED —
merged on the forge, not yet *done*.

TODO (deferred, souliane/teatree#2232): the ``spec_coverage`` manifest is carried
on the ticket (populated by the agent / a future ``ticket record-spec-coverage``
CLI), the same shape as ``fix_record`` / ``anti_vacuity_attestation``. Automatic
AC *extraction* — parsing the GitHub issue body / a linked spec doc into the
manifest — is NOT built here. This module ships the gate scaffold + the fail-loud
check + the carry mechanism; the AC-source auto-population is follow-up.
"""

import logging
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.models.errors import InvalidTransitionError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import AcceptanceCriterion

logger = logging.getLogger(__name__)


class SpecCoverageDodError(InvalidTransitionError):
    """A delivery transition was refused: an acceptance criterion lacked a backing test.

    A subclass of :class:`InvalidTransitionError` (sibling of
    :class:`~teatree.core.gates.fix_dod_gate.FixRecordDodError`) so the loop's
    ``Task.complete()`` outer atomic rolls the delivery advance back and the FSM
    stays at RETROSPECTED. The message names the uncovered ACs plus the override
    escape hatch so the operator can unblock a genuinely AC-less ticket.
    """


def spec_coverage_required() -> bool:
    """Whether the spec-coverage DoD gate is in force (overlay -> global)."""
    return get_effective_settings().require_spec_coverage


def acceptance_criteria(ticket: "Ticket") -> list["AcceptanceCriterion"]:
    """The declared acceptance-criteria mappings, or an empty list.

    A missing manifest, a non-mapping manifest, or a non-list
    ``acceptance_criteria`` all yield ``[]`` — there is no partial parse.
    Non-mapping entries inside the list are dropped.
    """
    manifest = (ticket.extra or {}).get("spec_coverage")
    if not isinstance(manifest, dict):
        return []
    criteria = manifest.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return []
    return [ac for ac in criteria if isinstance(ac, dict)]


def _ac_label(ac: "AcceptanceCriterion") -> str:
    """A human label for an AC: its ``id`` if present, else its ``description``."""
    return str(ac.get("id") or ac.get("description") or "<unnamed-ac>").strip()


def _ac_is_covered(ac: "AcceptanceCriterion") -> bool:
    """Whether an AC names at least one non-blank backing test reference."""
    tests = ac.get("tests")
    if not isinstance(tests, list):
        return False
    return any(str(t).strip() for t in tests)


def uncovered_acs(ticket: "Ticket") -> list[str]:
    """Return the labels of declared ACs that carry no backing test.

    An empty list means every declared AC is covered (or none are declared). A
    declared AC whose ``tests`` is absent, not a list, empty, or all-blank is
    uncovered — there is no partial credit per AC.
    """
    return [_ac_label(ac) for ac in acceptance_criteria(ticket) if not _ac_is_covered(ac)]


def has_full_coverage(ticket: "Ticket") -> bool:
    """Return True iff every declared AC carries at least one backing test."""
    return not uncovered_acs(ticket)


def override_reason(ticket: "Ticket") -> str:
    """The recorded escape-hatch reason, or ``""`` when no override is set."""
    override = (ticket.extra or {}).get("spec_coverage_override") or {}
    return str(override.get("reason", "")).strip()


def check_spec_coverage(ticket: "Ticket") -> None:
    """Refuse the delivery transition when an acceptance criterion lacks a backing test.

    Order of short-circuits (cheapest, most-permissive first):

    1. Gate off → pass (the opt-in default; existing delivery unchanged).
    2. A recorded override reason → pass (logged for audit).
    3. A manifest with every declared AC covered → pass.
    4. No manifest at all → raise (zero ACs proven is the partial subset the gate forecloses).
    5. One or more uncovered ACs → raise, naming them.
    """
    if not spec_coverage_required():
        return
    reason = override_reason(ticket)
    if reason:
        logger.info("Spec-coverage DoD gate overridden for ticket %s: %s", ticket.pk, reason)
        return

    criteria = acceptance_criteria(ticket)
    if not criteria:
        msg = (
            f"Refusing to mark ticket {ticket} done — its Definition of Done requires a "
            f"spec-coverage manifest mapping every acceptance criterion to a backing test, "
            f"and none is recorded. Declaring done on zero proven ACs is the partial-subset "
            f"claim this gate forecloses. Record the manifest in "
            f"`extra['spec_coverage']['acceptance_criteria']` (each AC an `id`/`description` "
            f"plus a non-empty `tests` list), or if this ticket genuinely has no acceptance "
            f"criteria record an override: "
            f"`extra['spec_coverage_override'] = {{'reason': '<why>'}}`."
        )
        raise SpecCoverageDodError(msg)

    uncovered = uncovered_acs(ticket)
    if not uncovered:
        return
    msg = (
        f"Refusing to mark ticket {ticket} done — these acceptance criteria have no backing "
        f"test: {', '.join(uncovered)}. Done cannot be declared on a partial subset of the "
        f"spec; every AC must map to at least one test in its `tests` list. Add the missing "
        f"tests and record them, or record an override with a stated reason in "
        f"`extra['spec_coverage_override']`."
    )
    raise SpecCoverageDodError(msg)
