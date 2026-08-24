"""Named failure kinds for a FAILED task — why it failed, not why it was scheduled.

``Task.execution_reason`` says why a task was DISPATCHED ("Auto-scheduled self-PR
review …"). Nothing said why it FAILED, so an operator reading a task listing or a
kanban card saw a cause-less error and could not tell a genuine review defect from a
lost lease, a bad harness pin, or an exhausted credential — they render identically.

This module is the vocabulary that fixes that, and it deliberately extends the
existing one rather than inventing a second: :mod:`teatree.agents.runner_failure_taxonomy`
already names a terminal run's outcome instead of emitting a generic string, because a
GENERIC reason was actively harmful — a capped run matched the transient marker set and
was auto-requeued straight back into the same ceiling. The same argument applies to a
task-level reason, so every reason resolves to a NAME here, including the two names that
exist to make a gap visible: :attr:`FailureKind.UNRECORDED` (a failure path that stored
nothing) and :attr:`FailureKind.UNCLASSIFIED` (a real reason this vocabulary has no name
for yet).

Both functions are pure functions of the recorded reason string, so the vocabulary is
testable without a task, a harness, or a database.

One table, two columns (souliane/teatree#4505)
----------------------------------------------
:data:`RECOVERY` maps every kind to a :class:`Recovery` — what to DO about the failure
(:class:`RecoveryStrategy`) and whose fault it was (:func:`is_environmental`, the operator's
diagnostic axis that makes a review defect distinguishable from a harness fault on the card).
Both used to be hand-maintained lists that never consulted each other: the requeue decision
lived in ``failure_signatures`` keyed on error TEXT, so a kind could be classified
environmental here and still never be retried. ``harness_crash`` was exactly that, and it
dropped eleven tasks in one day. Text markers now feed CLASSIFICATION only
(:func:`classify_failure`); the strategy is a property of the kind.

The two columns stay distinct, and the invariant is one-way: everything the sweep retries is
environmental, never the reverse. :attr:`FailureKind.LEASE_LOST` is environmental (a concurrent
re-claim, not a defect in the diff) yet is HALTed, because a live successor task already holds
the work and reopening the predecessor would duplicate it (souliane/teatree#3534). Collapsing
the columns would silently re-run one of them into a wall.

A third axis: causeless (souliane/teatree#4075)
-----------------------------------------------
:func:`is_causeless` answers a third question — "did this failure name a cause at all?".
A run that emitted no envelope, or that was cut off at its runtime ceiling, reported
NOTHING about why the work is unfinished, so two of them are one silence repeated rather
than one defect recurring. Feeding that to the repair loop's two-consecutive-identical
stall check makes "we learned nothing, twice" indistinguishable from "one defect recurred
twice" — and the corrective retry the loop itself schedules supplies the second strike, so
the halt is manufactured rather than observed. :func:`stall_fingerprints` therefore drops
them from the stall comparison; the attempt still burns iteration budget and still
escalates at the cap.

Membership is decided by that absence-of-a-cause test, NOT by whether the recorded text
happens to repeat: ``no_result_envelope`` is a module constant
(:data:`teatree.agents.envelope_refusal.NO_ENVELOPE_ERROR`), so it always self-collides on
the fingerprint, but ``runtime_ceiling``'s reason interpolates the breach (``ran 3601s``
vs ``ran 3722s`` fingerprint differently) and so does NOT collide by construction — for it,
the KIND-level drop in :func:`stall_kinds` is what does the work, and the fingerprint
side is only sometimes redundant with it.

Deliberately NARROWER than :data:`_UNNAMED`, the other set :func:`stall_kinds` drops: those
two kinds fail to name a cause because classification could not place a reason that IS
there, so the text still carries the
defect and the fingerprint check discriminates it (``UNRECORDED`` is the one exception —
its text is blank by definition, so its fingerprint is empty and already dropped by the
existing empty-fingerprint guard). A causeless kind's reason, in contrast, IS the reason —
there is no defect-specific text underneath it for a check to discriminate.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from django.db import models

from teatree.failure_signatures import HARNESS_CRASH_MARKERS, outage_signature_in_text

#: Prefix a lease-reaper failure carries, so the reaped row names its own cause.
LEASE_EXPIRED_PREFIX = "lease_expired: "

#: Prefix an operator-initiated cancellation carries.
CANCELLED_PREFIX = "cancelled: "

#: Prefix a task cancelled because its ticket was reworked carries.
SUPERSEDED_PREFIX = "superseded: "

#: Prefix an agent-initiated failure that named no cause of its own carries.
AGENT_ABANDONED_PREFIX = "agent_abandoned: "


class FailureKind(models.TextChoices):
    """The named cause of a FAILED task, stamped on the attempt that failed it."""

    UNRECORDED = "unrecorded", "No reason recorded"
    UNCLASSIFIED = "unclassified", "Unclassified"
    LEASE_LOST = "lease_lost", "Lease lost to another worker"
    LEASE_EXPIRED = "lease_expired", "Lease expired and was reaped"
    RUNTIME_CEILING = "runtime_ceiling", "Runtime ceiling exceeded"
    USAGE_LIMIT_PARKED = "usage_limit_parked", "Parked on a usage window"
    CREDENTIAL_EXHAUSTED = "credential_exhausted", "Credentials exhausted"
    HARNESS_CONFIG_INVALID = "harness_config_invalid", "Invalid harness configuration"
    HARNESS_CRASH = "harness_crash", "Harness crashed"
    OUTAGE = "outage", "Network or API outage"
    RESULT_ERROR = "result_error", "Run ended without a clean result"
    PROVISION_FAILED = "provision_failed", "Worktree provisioning failed"
    LANDING_UNVERIFIED = "landing_unverified", "Work never landed"
    NO_RESULT_ENVELOPE = "no_result_envelope", "No result envelope produced"
    EVIDENCE_MISSING = "evidence_missing", "Required evidence missing"
    RECORDING_REFUSED = "recording_refused", "Recording refused by a gate"
    PLAN_MISSING = "plan_missing", "No plan recorded before an implementing dispatch"
    CANCELLED = "cancelled", "Cancelled by an operator"
    SUPERSEDED = "superseded", "Superseded by rework"
    AGENT_ABANDONED = "agent_abandoned", "Agent failed the task without a reason"


class RecoveryStrategy(StrEnum):
    """What a FAILED task of a given kind has earned — the recovery half of :data:`RECOVERY`."""

    RETRY = "retry"
    CORRECTIVE_RETRY = "corrective_retry"
    HALT = "halt_and_escalate"


@dataclass(frozen=True)
class Recovery:
    """One kind's row: what to do about it, and whose fault it was."""

    strategy: RecoveryStrategy
    environmental: bool


_RETRY = RecoveryStrategy.RETRY
_CORRECT = RecoveryStrategy.CORRECTIVE_RETRY
_HALT = RecoveryStrategy.HALT

#: The one table both axes read (souliane/teatree#4505). Total over :class:`FailureKind` — a kind
#: added without a row fails ``tests/teatree_core/modelkit/test_task_failure_taxonomy.py``, so the
#: gap that dropped every ``harness_crash`` cannot recur silently.
RECOVERY: Mapping[str, Recovery] = {
    # An infrastructure interruption: the work never got its chance, so reopen it. Safe because the
    # sweep is bounded twice — the #2009 iteration cap, and a loud halt on two identical failures.
    FailureKind.OUTAGE: Recovery(_RETRY, environmental=True),
    FailureKind.RESULT_ERROR: Recovery(_RETRY, environmental=True),
    FailureKind.PROVISION_FAILED: Recovery(_RETRY, environmental=True),
    FailureKind.LANDING_UNVERIFIED: Recovery(_RETRY, environmental=True),
    FailureKind.HARNESS_CRASH: Recovery(_RETRY, environmental=True),
    # A bounded correction exists; whether THIS task earns it is the sweep's own one-shot decision.
    FailureKind.NO_RESULT_ENVELOPE: Recovery(_CORRECT, environmental=False),
    FailureKind.EVIDENCE_MISSING: Recovery(_CORRECT, environmental=False),
    FailureKind.RECORDING_REFUSED: Recovery(_CORRECT, environmental=False),
    FailureKind.HARNESS_CONFIG_INVALID: Recovery(_CORRECT, environmental=False),
    # Environmental, yet reopening races whoever already owns the work, or a window that has not
    # reset — so these page a human instead.
    FailureKind.LEASE_LOST: Recovery(_HALT, environmental=True),
    FailureKind.LEASE_EXPIRED: Recovery(_HALT, environmental=True),
    FailureKind.USAGE_LIMIT_PARKED: Recovery(_HALT, environmental=True),
    FailureKind.CREDENTIAL_EXHAUSTED: Recovery(_HALT, environmental=True),
    # A defect, a deliberate stop, or a failure this vocabulary cannot name: never auto-reopened.
    # PLAN_MISSING is HALT because re-running the SAME implementing phase reproduces it exactly —
    # the remedy is a different phase (planning), which `unplanned_ticket_redispatch` schedules off
    # this very name rather than off the reason text (souliane/teatree#4578).
    FailureKind.PLAN_MISSING: Recovery(_HALT, environmental=False),
    FailureKind.UNRECORDED: Recovery(_HALT, environmental=False),
    FailureKind.UNCLASSIFIED: Recovery(_HALT, environmental=False),
    FailureKind.RUNTIME_CEILING: Recovery(_HALT, environmental=False),
    FailureKind.CANCELLED: Recovery(_HALT, environmental=False),
    FailureKind.SUPERSEDED: Recovery(_HALT, environmental=False),
    FailureKind.AGENT_ABANDONED: Recovery(_HALT, environmental=False),
}

#: Kinds that are the ABSENCE of a cause rather than a cause. Membership is that test, NOT
#: fingerprint collision — ``no_result_envelope``'s constant reason self-collides,
#: ``runtime_ceiling``'s interpolated one does not. See the module docstring: dropped from
#: the stall comparison, never from the iteration budget.
_CAUSELESS: frozenset[str] = frozenset(
    {
        FailureKind.NO_RESULT_ENVELOPE,
        FailureKind.RUNTIME_CEILING,
    },
)

#: Kinds that are the ABSENCE of a NAME rather than a cause, so two of them are two
#: unrelated failures rather than one repeating defect. See the module docstring on why
#: this is deliberately WIDER than :data:`_CAUSELESS`: the defect's own text is still
#: there underneath, so the fingerprint check discriminates them and is kept.
_UNNAMED: frozenset[str] = frozenset(
    {
        FailureKind.UNCLASSIFIED,
        FailureKind.UNRECORDED,
    },
)

# Ordered most-specific-first: the first matching predicate names the failure. Order is
# load-bearing where prefixes overlap — a ``stuck_loop:`` reason is a LOST LEASE or a
# RUNTIME CEILING, and only the former is environmental, so the lease phrase must be
# tested before the generic prefix claims the reason.
_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (FailureKind.LEASE_EXPIRED, (LEASE_EXPIRED_PREFIX,)),
    (FailureKind.CANCELLED, (CANCELLED_PREFIX,)),
    (FailureKind.SUPERSEDED, (SUPERSEDED_PREFIX,)),
    (FailureKind.AGENT_ABANDONED, (AGENT_ABANDONED_PREFIX,)),
    (FailureKind.LEASE_LOST, ("stuck_loop: lease lost",)),
    (FailureKind.RUNTIME_CEILING, ("stuck_loop:",)),
    (FailureKind.USAGE_LIMIT_PARKED, ("limit_parked:",)),
    (FailureKind.OUTAGE, ("outage_death:",)),
    (FailureKind.RESULT_ERROR, ("result_error:",)),
    (FailureKind.PROVISION_FAILED, ("provision_failed:",)),
    (FailureKind.LANDING_UNVERIFIED, ("landing_unverified:",)),
    (FailureKind.NO_RESULT_ENVELOPE, ("no_result_envelope:",)),
    (FailureKind.PLAN_MISSING, ("plan_missing:",)),
    (FailureKind.CREDENTIAL_EXHAUSTED, ("accounts are exhausted", "credit balance is too low")),
    (FailureKind.HARNESS_CONFIG_INVALID, ("is not valid for agent_harness", "agent_harness_provider=")),
    (FailureKind.EVIDENCE_MISSING, ("missing required evidence",)),
    # The recorder's other three refusals of a parsed-but-unusable envelope were unnamed until
    # #4505; leaving them ``unclassified`` denied them the correction their sibling earns.
    (
        FailureKind.RECORDING_REFUSED,
        ("recording refused", "unexpected keys", "result is not valid json", "result must be a json object"),
    ),
    (FailureKind.HARNESS_CRASH, HARNESS_CRASH_MARKERS),
)


def classify_failure(error: str) -> str:
    """Name the cause recorded in *error* — always a :class:`FailureKind`, never blank.

    A blank reason is :attr:`FailureKind.UNRECORDED`: the failure path that produced it
    stored nothing, which is a defect in that path rather than a property of the failure,
    and naming it is what makes it findable instead of invisible.

    An unmatched non-blank reason is :attr:`FailureKind.UNCLASSIFIED` — deliberately NOT
    environmental, so an unrecognised failure is never presented as a harness fault the
    operator can dismiss.
    """
    haystack = error.casefold().strip()
    if not haystack:
        return FailureKind.UNRECORDED
    for kind, needles in _MATCHERS:
        if any(needle.casefold() in haystack for needle in needles):
            return kind
    if outage_signature_in_text(error):
        return FailureKind.OUTAGE
    return FailureKind.UNCLASSIFIED


def recovery_strategy(kind: str) -> RecoveryStrategy:
    """What *kind* has earned. A kind this build cannot place HALTs — never auto-reopened."""
    recovery = RECOVERY.get(kind)
    return recovery.strategy if recovery is not None else RecoveryStrategy.HALT


def is_environmental(kind: str) -> bool:
    """Whether *kind* is an environment/infrastructure fault rather than a defect.

    The operator axis only — see the module docstring on why this is deliberately a
    superset of the retried set and must not be substituted for it.
    """
    recovery = RECOVERY.get(kind)
    return recovery is not None and recovery.environmental


def is_causeless(kind: str) -> bool:
    """Whether *kind* is the absence of a cause, so two of them are not one repeating defect.

    The stall axis only — see the module docstring on why this is narrower than the
    "absence of a NAME" set the kind check drops.
    """
    return kind in _CAUSELESS


def stall_fingerprints(kind_fingerprints: Iterable[tuple[str, str]]) -> list[str]:
    """The ``(kind, fingerprint)`` pairs' fingerprints that count toward the stall check.

    The single builder every ``last_two_fingerprints`` caller shares, so a causeless
    failure can never be dropped on one repair path and counted on another.
    """
    return [fingerprint for kind, fingerprint in kind_fingerprints if fingerprint and not is_causeless(kind)]


def stall_kinds(kinds: Iterable[str]) -> list[str]:
    """The failure kinds that count toward the NAMED-DETERMINISTIC stall check (#3957).

    The kind-side sibling of :func:`stall_fingerprints`, and the single builder every
    ``last_two_deterministic_kinds`` caller shares. This is the mechanism that actually
    carries ``runtime_ceiling``: its reason interpolates the breach, so two of them
    fingerprint DIFFERENTLY and the fingerprint filter never sees a collision to drop.

    Dropping rather than substituting a placeholder is load-bearing — one dropped kind
    between two identical named ones leaves them non-adjacent, so only two CONSECUTIVE
    named deterministic failures halt.
    """
    return [
        kind
        for kind in kinds
        if kind and kind not in _UNNAMED and not is_causeless(kind) and not is_environmental(kind)
    ]


__all__ = [
    "AGENT_ABANDONED_PREFIX",
    "CANCELLED_PREFIX",
    "LEASE_EXPIRED_PREFIX",
    "RECOVERY",
    "SUPERSEDED_PREFIX",
    "FailureKind",
    "Recovery",
    "RecoveryStrategy",
    "classify_failure",
    "is_causeless",
    "is_environmental",
    "recovery_strategy",
    "stall_fingerprints",
    "stall_kinds",
]
