"""Rubric->verifier done-gate on the keystone merge precondition (#2241).

The hole this forecloses: a ticket can reach MERGED with its acceptance criteria
unverified — the recurring "declared done on a 2xx / a partial subset / an unrun
test" failure. The standing rule "declare done only on a verified, full-spec
outcome" is prose + memory; neither mechanically refuses the merge when the
rubric is not fully PASS by an independent verifier.

This is the structural gate. It extends the §17.4.3 keystone-merge precondition
family (sibling of the #1829 anti-vacuity gate) with one dimension: the ticket's
:class:`teatree.core.models.rubric.Rubric` must be fully PASS — every criterion
graded PASS by a positively-identified independent grader
(``is_independent_reviewer_identity``), bound to
the merge-time live head SHA, each PASS citing what proves it. It is **fail-closed**:
an empty, ungraded, failed, uncited, maker-graded, or stale-SHA rubric is treated as
not-passed and the merge is refused. It never skip-as-passes (the standing "gate must fail loud" rule).

SHA-binding mirrors ``MergeClear.reviewed_sha`` and the anti-vacuity attestation:
each grade records the ``reviewed_sha`` it was produced against, so when the live
head moves off it (force-push, new commits) the recorded grade is treated as stale
and the rubric must be re-graded — closing the replay window where a grade for an
old tree authorises a later, unverified one.

The gate runs on TWO transitions, and there is no setting that relaxes either. At
``merge`` :func:`check_rubric_satisfied` additionally binds to the live head SHA. At
``mark_delivered`` :func:`check_rubric_verified` drops that bind — a delivered ticket's
head has long since moved on, and re-binding there would refuse every delivery — but
keeps every other condition. It is the successor to the deleted spec-coverage DoD gate:
a missing rubric blocks exactly as a missing manifest did, and the audited waiver is the
human-authorized ``ticket plan-bypass`` — which waives a missing rubric and every rung
except a recorded FAIL, because a bypass says "there was nothing to declare", never "the
verifier's FAIL does not count".

Both are pure functions over the durable rubric row, mirroring
:mod:`teatree.core.gates.anti_vacuity_gate`. The merge block raises
:class:`RubricNotSatisfiedError`, which the merge precondition gate re-wraps as a
``MergePreconditionError``; the delivery block raises :class:`RubricNotVerifiedError`,
an ``InvalidTransitionError`` so the loop's outer atomic rolls the advance back and the
ticket stays RETRO_RECORDED — merged on the forge, not yet *done*.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.honesty_escalation import HonestyEscalation
from teatree.core.models.plan_adequacy import is_plan_bypass_shaped
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.rubric import Rubric

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

_NO_RUBRIC_REASON = (
    "no rubric is recorded for this ticket — declaring done on zero proven acceptance "
    "criteria is the partial-subset claim this gate forecloses"
)

_REMEDY = (
    "Have an INDEPENDENT verifier (grader != maker) grade the criteria with `t3 <overlay> ticket "
    'rubric-grade {pk} --grader-identity <reviewer> --reviewed-sha <full-40-char-sha> --grades-json \'[{{"ordinal": '
    '0, "status": "pass", "rationale": "<what proves it>"}}, ...]\'`. The criteria come from the plan\'s '
    "`acceptance_criteria` section; a ticket with genuinely nothing to grade records "
    "`ticket plan-bypass --human-authorize`."
)


class RubricNotSatisfiedError(RuntimeError):
    """A merge was refused because the ticket's rubric is not fully PASS at the head SHA."""


class RubricNotVerifiedError(InvalidTransitionError):
    """A delivery was refused because the ticket's rubric is not fully PASS."""


def latest_rubric(ticket: "Ticket") -> "Rubric | None":
    """The ticket's active rubric (most-recently-created), or ``None``.

    A ticket has at most one active rubric (``populate`` is a get-or-create), so
    the manager's ``active_for_ticket`` (order by ``-created_at``, first) is the
    active row.
    """
    return Rubric.objects.active_for_ticket(ticket)


def _plan_is_bypassed(ticket: "Ticket") -> bool:
    """Whether the ticket's LATEST plan is the human-authorized bypass — the ONE waiver."""
    latest = PlanArtifact.objects.filter(ticket=ticket).order_by("-recorded_at", "-pk").first()
    return latest is not None and is_plan_bypass_shaped(latest.adequacy)


def _refusal_reason(ticket: "Ticket", head_sha: str | None) -> str:
    """Why the ticket's rubric refuses this transition, or ``""``.

    *head_sha* binds the grade to the live tree (merge) or is ``None`` (delivered,
    whose head has long since moved). A bypassed plan waives the missing rubric and
    every rung except a recorded FAIL.
    """
    waived = _plan_is_bypassed(ticket)
    rubric = latest_rubric(ticket)
    if rubric is None:
        return "" if waived else _NO_RUBRIC_REASON
    return rubric.unverified_reason(head_sha, waived=waived)


def _record_shipped_incomplete_escalation(ticket: "Ticket") -> None:
    """Write a ``shipped_incomplete`` honesty escalation for the ticket's session (#2263).

    The deterministic backstop for trigger #4 (shipped a job not verified
    complete): when the rubric done-gate REFUSES a merge, the work that reached
    this point was shipped without a verified-complete rubric, so the ticket's
    active session is escalated to the most-honest model for its next
    verification spawn. Keyed to the ticket's most-recent session ``agent_id``
    (ticket-wide, ``task_id=None``). Fail-SAFE: any error recording the row is
    swallowed — the backstop must never block the (already-refusing) gate.
    """
    try:
        session = ticket.sessions.exclude(agent_id="").order_by("-started_at").first()
        if session is not None and session.agent_id:
            HonestyEscalation.record(HonestyEscalation.Reason.SHIPPED_INCOMPLETE, session_id=session.agent_id)
    except Exception:  # noqa: BLE001 — the honesty backstop must never block the already-refusing gate
        return


def clear_honesty_escalation_on_pass(ticket: "Ticket") -> None:
    """Clear the ticket's active honesty escalations on a verified-complete landing.

    The mirror of :func:`_record_shipped_incomplete_escalation` above, and why it lives
    here: this gate is what RECORDS ``shipped_incomplete`` when the rubric refuses, so
    the clear on a full pass belongs beside it rather than in one of the two producers
    that call it. Keyed to the ticket's session ``agent_id``s. Fail-SAFE: a recording
    error never blocks the caller — the grade is already recorded, this is cleanup.
    """
    try:
        sessions = ticket.sessions.exclude(agent_id="")
        for agent_id in sessions.values_list("agent_id", flat=True).distinct():
            HonestyEscalation.mark_cleared(agent_id)
    except Exception:  # noqa: BLE001 — best-effort side-effect; a failure degrades to no-op
        return


def check_rubric_satisfied(ticket: "Ticket", head_sha: str, *, transition: str) -> None:
    """Refuse a ``transition`` whose ticket rubric is not fully PASS at ``head_sha``.

    Fail-closed: a missing, empty, ungraded, failed, uncited, maker-graded or
    stale-SHA rubric is refused. ``transition`` names the gated action for the message.

    On a refusal it also records a ``shipped_incomplete`` honesty escalation
    (teatree#2263 trigger #4 backstop) for the ticket's active session before
    raising, so the next verification spawn routes to the most-honest model.
    """
    reason = _refusal_reason(ticket, head_sha)
    if not reason:
        return
    _record_shipped_incomplete_escalation(ticket)
    short_sha = head_sha.strip()[:8] or head_sha.strip()
    msg = (
        f"refusing the '{transition}' transition for ticket {ticket.pk} at head {short_sha}: {reason}. "
        + _REMEDY.format(pk=ticket.pk)
    )
    raise RubricNotSatisfiedError(msg)


def check_rubric_verified(ticket: "Ticket") -> None:
    """Refuse ``mark_delivered`` when the ticket's rubric is not fully verified.

    The delivered-time sibling of :func:`check_rubric_satisfied`, minus the head bind:
    a delivered ticket's head has long since moved off the reviewed one, so re-binding
    here would refuse every delivery.
    """
    reason = _refusal_reason(ticket, None)
    if not reason:
        return
    msg = f"Refusing to mark ticket {ticket.pk} done — {reason}. " + _REMEDY.format(pk=ticket.pk)
    raise RubricNotVerifiedError(msg)


register_gate("rubric_verified", check_rubric_verified)
