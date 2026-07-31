"""Named failure kinds for a FAILED task — why it failed, not why it was scheduled.

``Task.execution_reason`` says why a task was DISPATCHED ("Auto-scheduled self-PR
review …"). Nothing said why it FAILED, so an operator reading a task listing or a
kanban card saw a cause-less error and could not tell a genuine review defect from a
lost lease, a bad harness pin, or an exhausted credential — they render identically.

This module is the vocabulary that fixes that, and it deliberately extends the
existing one rather than inventing a second: :mod:`teatree.agents.headless_failure_taxonomy`
already names a terminal run's outcome instead of emitting a generic string, because a
GENERIC reason was actively harmful — a capped run matched the transient marker set and
was auto-requeued straight back into the same ceiling. The same argument applies to a
task-level reason, so every reason resolves to a NAME here, including the two names that
exist to make a gap visible: :attr:`FailureKind.UNRECORDED` (a failure path that stored
nothing) and :attr:`FailureKind.UNCLASSIFIED` (a real reason this vocabulary has no name
for yet).

Both functions are pure functions of the recorded reason string, so the vocabulary is
testable without a task, a harness, or a database.

Two axes, deliberately NOT the same axis
----------------------------------------
:func:`is_environmental` answers the OPERATOR's question — "was this the work's fault,
or the environment's?" — which is what makes a review defect distinguishable from a
harness fault on the card.

It is NOT the requeue predicate. That remains
:func:`teatree.agents.outage_classifier.is_transient_failure`, and the two are related by
a one-way invariant this module's tests pin: everything the requeue sweep calls transient
is also environmental here, never the reverse. The gap between them is intentional —
:attr:`FailureKind.LEASE_LOST` is environmental (a concurrent re-claim, not a defect in
the diff) yet must NOT be requeued by that sweep, because a live successor task already
holds the work and reopening the predecessor would duplicate it (souliane/teatree#3534).
Collapsing the two axes would silently re-run one of them into a wall.
"""

from django.db import models

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
    CANCELLED = "cancelled", "Cancelled by an operator"
    SUPERSEDED = "superseded", "Superseded by rework"
    AGENT_ABANDONED = "agent_abandoned", "Agent failed the task without a reason"


#: Kinds caused by the environment rather than by a defect in the work. See the module
#: docstring: this is the operator's diagnostic axis, NOT the requeue predicate.
_ENVIRONMENTAL: frozenset[str] = frozenset(
    {
        FailureKind.LEASE_LOST,
        FailureKind.LEASE_EXPIRED,
        FailureKind.USAGE_LIMIT_PARKED,
        FailureKind.CREDENTIAL_EXHAUSTED,
        FailureKind.HARNESS_CRASH,
        FailureKind.OUTAGE,
        FailureKind.RESULT_ERROR,
        FailureKind.PROVISION_FAILED,
        FailureKind.LANDING_UNVERIFIED,
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
    (FailureKind.CREDENTIAL_EXHAUSTED, ("accounts are exhausted", "credit balance is too low")),
    (FailureKind.HARNESS_CONFIG_INVALID, ("is not valid for agent_harness", "agent_harness_provider=")),
    (FailureKind.EVIDENCE_MISSING, ("missing required evidence",)),
    (FailureKind.RECORDING_REFUSED, ("recording refused",)),
    (FailureKind.HARNESS_CRASH, ("traceback (most recent call last)", "processerror")),
    # A raw connection signature whose envelope was never stamped with a marker. These
    # mirror ``teatree.agents.outage_classifier``; this module is a pure leaf (it may not
    # import the agents layer), so a test pins the two against each other rather than a
    # runtime call doing it.
    (
        FailureKind.OUTAGE,
        (
            "unable to connect to api",
            "connectionrefused",
            "connection refused",
            "failedtoopensocket",
            "failed to open socket",
            "safety classifier unavailable",
        ),
    ),
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
    return FailureKind.UNCLASSIFIED


def is_environmental(kind: str) -> bool:
    """Whether *kind* is an environment/infrastructure fault rather than a defect.

    The operator axis only — see the module docstring on why this is deliberately a
    superset of the requeue sweep's transient set and must not be substituted for it.
    """
    return kind in _ENVIRONMENTAL


__all__ = [
    "AGENT_ABANDONED_PREFIX",
    "CANCELLED_PREFIX",
    "LEASE_EXPIRED_PREFIX",
    "SUPERSEDED_PREFIX",
    "FailureKind",
    "classify_failure",
    "is_environmental",
]
