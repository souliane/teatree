"""Rubric->verifier done-gate on the keystone merge precondition (#2241).

The hole this forecloses: a ticket can reach MERGED with its acceptance criteria
unverified — the recurring "declared done on a 2xx / a partial subset / an unrun
test" failure. The standing rule "declare done only on a verified, full-spec
outcome" is prose + memory; neither mechanically refuses the merge when the
rubric is not fully PASS by an independent verifier.

This is the structural gate. It extends the §17.4.3 keystone-merge precondition
family (sibling of the #1829 anti-vacuity gate) with one dimension: the ticket's
:class:`teatree.core.models.rubric.Rubric` must be fully PASS — every criterion
graded PASS by a grader that is NOT the maker (``is_non_reviewer_role``), bound to
the merge-time live head SHA. It is **fail-closed**: an empty, ungraded, failed,
maker-graded, or stale-SHA rubric is treated as not-passed and the merge is
refused. It never skip-as-passes (the standing "gate must fail loud" rule).

SHA-binding mirrors ``MergeClear.reviewed_sha`` and the anti-vacuity attestation:
each grade records the ``reviewed_sha`` it was produced against, so when the live
head moves off it (force-push, new commits) the recorded grade is treated as stale
and the rubric must be re-graded — closing the replay window where a grade for an
old tree authorises a later, unverified one.

``require_rubric_verification`` is ``False`` unless configured. With it unset the
gate is a NO-OP — projects that do not require rubric verification keep merging
unchanged. The gate is a pure function over the durable rubric row plus the live
head SHA, mirroring :mod:`teatree.core.gates.anti_vacuity_gate`. On a block it
raises :class:`RubricNotSatisfiedError` with a remediation naming the
``rubric-set`` / ``rubric-grade`` commands; the merge precondition gate surfaces it
as a refusal (re-wrapped as a ``MergePreconditionError``).
"""

from typing import TYPE_CHECKING

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from teatree.core.models.rubric import Rubric
    from teatree.core.models.ticket import Ticket


class RubricNotSatisfiedError(RuntimeError):
    """A merge was refused because the ticket's rubric is not fully PASS at the head SHA."""


def rubric_gate_required(overlay: str | None = None) -> bool:
    """Whether the rubric done-gate is in force for *overlay* (overlay -> global).

    *overlay* threads the ticket's own overlay so a per-overlay opt-in binds even
    when the evaluating process has no ambient ``T3_OVERLAY_NAME`` (the merge
    keystone runs env-less). ``None`` resolves the ambient overlay as before.
    """
    return get_effective_settings(overlay).require_rubric_verification


def latest_rubric(ticket: "Ticket") -> "Rubric | None":
    """The ticket's active rubric (most-recently-created), or ``None``.

    A ticket has at most one active rubric (``populate`` is a get-or-create), so
    the manager's ``active_for_ticket`` (order by ``-created_at``, first) is the
    active row.
    """
    from teatree.core.models.rubric import Rubric  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return Rubric.objects.active_for_ticket(ticket)


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
    from teatree.core.models.honesty_escalation import HonestyEscalation  # noqa: PLC0415 — deferred: ORM/app-registry

    try:
        session = ticket.sessions.exclude(agent_id="").order_by("-started_at").first()  # ty: ignore[unresolved-attribute]
        if session is not None and session.agent_id:
            HonestyEscalation.record(HonestyEscalation.Reason.SHIPPED_INCOMPLETE, session_id=session.agent_id)
    except Exception:  # noqa: BLE001 — the honesty backstop must never block the already-refusing gate
        return


def check_rubric_satisfied(ticket: "Ticket", head_sha: str, *, transition: str) -> None:
    """Refuse a ``transition`` whose ticket rubric is not fully PASS at ``head_sha``.

    NO-OP when ``require_rubric_verification`` is off (the opt-in default).
    Otherwise the ticket must carry a rubric that
    :meth:`Rubric.is_fully_passed_at` accepts — every criterion PASS by an
    independent grader bound to ``head_sha``. Fail-closed: a missing, empty,
    ungraded, failed, maker-graded, or stale-SHA rubric is refused. ``transition``
    names the gated action (e.g. ``"merge"``) for the remediation message.

    On a refusal it also records a ``shipped_incomplete`` honesty escalation
    (teatree#2263 trigger #4 backstop) for the ticket's active session before
    raising, so the next verification spawn routes to the most-honest model.
    """
    if not rubric_gate_required(ticket.overlay or None):
        return
    rubric = latest_rubric(ticket)
    if rubric is not None and rubric.is_fully_passed_at(head_sha):
        return
    _record_shipped_incomplete_escalation(ticket)
    reason = rubric.block_reason(head_sha) if rubric is not None else "no rubric is recorded for this ticket"
    short_sha = head_sha.strip()[:8] or head_sha.strip()
    msg = (
        f"refusing the '{transition}' transition for ticket {ticket.pk} at head "
        f"{short_sha}: {reason} (require_rubric_verification). The ticket's "
        f"acceptance-criteria rubric must be fully PASS, graded by an INDEPENDENT "
        f"verifier (grader != maker) at the current head SHA. Set the criteria with "
        f"`ticket rubric-set {ticket.pk} --criteria-json '[\"AC1\", ...]'`, then have "
        f"the verifier grade them with `ticket rubric-grade {ticket.pk} --grader-identity "
        f"<reviewer> --reviewed-sha <full-40-char-sha> --grades-json "
        f'\'[{{"ordinal": 0, "status": "pass"}}, ...]\'`, then retry.'
    )
    raise RubricNotSatisfiedError(msg)
