"""Disposition of a FAILED task the requeue sweep must neither reopen nor escalate.

The half of :mod:`teatree.loop.transient_requeue` that answers "is this row's work
over, or is someone else finishing it?" — split out because it is a distinct concern
from the sweep's routing and budget, and the two together exceeded the module LOC cap.

Two dispositions, differing in whether the phase's work is done. A DEAD ARTIFACT is
retired COMPLETED — nothing can still land, so the row's replayed transition is inert.
A row with a LIVE SUCCESSOR is parked FAILED — its phase is unfinished and someone else
is finishing it, so marking it COMPLETED would advance the ticket over the successor.
"""

from teatree.core.modelkit.phase_tools import VERDICT_REVIEW_PHASES
from teatree.core.modelkit.phases import normalize_phase, phase_spellings
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, Ticket
from teatree.core.models.phase_landing import phase_landing_evidence

#: Stamped onto ``execution_reason`` when a SUPERSEDED FAILED task is retired (its
#: phase output the ticket's FSM already reached). It is marked COMPLETED and drops
#: out of the scan — the away-mode queue is never asked about a phase the ticket
#: already advanced past (the 3366/3336/3352 redispatch-loop root cause).
SUPERSEDED_STAMP = "[superseded-retired]"
#: Stamped onto ``execution_reason`` when a FAILED task is parked because a newer,
#: still-active sibling Task holds its ``(ticket, phase)`` (#3534). The row stays
#: FAILED — the phase has not completed — and drops out of the scan, so the stale
#: predecessor neither escalates nor advances the ticket's FSM.
LIVE_SUCCESSOR_STAMP = "[superseded-parked]"

#: Stamped onto ``execution_reason`` when a review/codex-review task is retired
#: because its linked PR is provably MERGED/CLOSED. A review verdict can never land
#: on a dead PR, so re-dispatching only burns a session that re-confirms the close
#: (#3556). The task is marked COMPLETED and the reviewer ticket is IGNORED so it
#: drops out of every active scan instead of re-dispatching indefinitely.
DEAD_REVIEW_STAMP = "[dead-review-retired]"


def dispose_without_reopen(task: Task) -> bool:
    """Dispose of a FAILED row the sweep must neither reopen nor escalate; ``True`` if handled.

    Two dispositions, differing in whether the phase's work is over. A DEAD ARTIFACT is
    retired COMPLETED — nothing can still land, so the row's transition is inert. A row
    with a LIVE SUCCESSOR is parked FAILED — its phase is unfinished and someone else is
    finishing it, so marking it COMPLETED would advance the ticket over the successor.
    """
    if _retire_if_dead_artifact(task):
        return True
    if _has_live_successor(task):
        _park_live_successor(task)
        return True
    return False


def _retire_if_dead_artifact(task: Task) -> bool:
    """Retire a FAILED task whose phase output is already moot; ``True`` if retired.

    Two ways a FAILED row becomes a dead artifact to retire (COMPLETED) rather than
    reopen or escalate:

    * SUPERSEDED — the phase's output demonstrably landed
        (:func:`~teatree.core.models.phase_landing.phase_landing_evidence` — the FULL author
        ladder, plus the artifact half ONLY for a lease-loss failure; see the module docstring).
    * DEAD REVIEW TARGET — a review/codex-review phase whose linked PR is
        merged/closed, so a verdict can never land; re-dispatching only burns a
        session that re-confirms the close (#3556).

    Both triggers are FSM-inert by construction — the ticket has already reached (or is
    being moved to) its terminal answer, so the COMPLETED row's replayed transition
    finds no matching guard. A live-successor row is NOT one of them: its phase is
    unfinished, so it is parked FAILED by :func:`_park_live_successor` instead.
    """
    if task.ticket.has_completed_phase(task.phase) or phase_landing_evidence(
        task, trust_phase_artifact=task.failure_kind == FailureKind.LEASE_LOST
    ):
        _retire_superseded(task)
        return True
    if _review_target_dead(task):
        _retire_dead_review(task)
        return True
    return False


def _has_live_successor(task: Task) -> bool:
    """Whether a newer, still-active sibling Task is handling *task*'s ``(ticket, phase)`` (#3534).

    A stuck-phase redispatch mints a FRESH Task for the same ``(ticket, phase)`` and can
    re-claim the predecessor's lease out from under it. The predecessor then lands FAILED
    carrying a ``stuck_loop: lease lost … re-claimed`` breach even though the phase is
    recovering fine under the successor — so escalating it files a ``DeferredQuestion``
    that was already stale at write time (the only correct answer was "ignore"). A
    later-pk sibling in an ACTIVE state (PENDING/CLAIMED) is that live successor. Only a
    strictly LATER row (``pk__gt``) counts, so the newest FAILED row is never parked on
    the strength of an older sibling — a genuinely blocked phase whose last row has no
    successor still escalates.

    Checked on ANY failure class, not just the lease loss that motivated it: the breach
    string is an unreliable discriminator (the same handoff surfaces under several
    wordings), and a live successor makes the predecessor's error moot whatever it was.
    The cost is that a parked row skips the budget-halt check, which the escalation path
    would otherwise run. That is bounded: the park grants no retry (the row is never
    reopened), its ``TaskAttempt`` rows survive so ``phase_attempts`` still counts them
    against the phase budget, and the successor carries the redispatcher's own cap — so a
    doomed phase still hits its budget through the successor's own row.
    """
    return Task.objects.filter(
        ticket_id=task.ticket_id,  # ty: ignore[unresolved-attribute]
        phase__in=phase_spellings(normalize_phase(task.phase)),
        status__in=Task.Status.active(),
        pk__gt=task.pk,
    ).exists()


def _park_live_successor(task: Task) -> None:
    """Park a FAILED task whose ``(ticket, phase)`` a live successor now holds. Idempotent.

    Stamps :data:`LIVE_SUCCESSOR_STAMP` so the row drops out of the sweep's FAILED scan
    and is never escalated again, via the same ``UPDATE ... WHERE status=FAILED``
    compare-and-swap the reopen/retire paths use.

    The row stays FAILED on purpose. Its phase did not finish — the successor is still
    mid-flight — so marking it COMPLETED (the :func:`_retire_superseded` shape) would make
    it the ticket's newest completed task and ``Task.objects.replay_orphaned_transitions``
    would fire its phase transition on the next tick, advancing the ticket past work
    nobody landed (a PLANNED ticket to CODED, a TESTED one through ``review()``).
    """
    if LIVE_SUCCESSOR_STAMP in task.execution_reason:
        return
    reason = (
        f"{task.execution_reason}\n{LIVE_SUCCESSOR_STAMP}".strip() if task.execution_reason else LIVE_SUCCESSOR_STAMP
    )
    Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(execution_reason=reason)


def _review_target_dead(task: Task) -> bool:
    """Whether *task* is a review phase whose linked PR is provably MERGED/CLOSED (#3556).

    Only a verdict-review phase consults the forge; every other phase returns
    ``False`` without a network read. The linked PR is the reviewer ticket's own
    ``issue_url`` (a codex/review ticket is keyed on the PR url). Fail-OPEN via
    :func:`~teatree.backends.loader.pr_is_merged_or_closed`: an UNKNOWN/indefinite
    state returns ``False`` so a transient forge hiccup never retires a live review.
    """
    if normalize_phase(task.phase) not in VERDICT_REVIEW_PHASES:
        return False
    from teatree.backends.loader import pr_is_merged_or_closed  # noqa: PLC0415 - deferred: backends/core cycle

    return pr_is_merged_or_closed(task.ticket.issue_url)


def _retire_dead_review(task: Task) -> None:
    """Retire a dead-review task COMPLETED and IGNORE its reviewer ticket. Idempotent.

    The review target is merged/closed, so the phase output can never land - mark the
    task COMPLETED (dropping it out of the FAILED scan) via the same
    ``UPDATE ... WHERE status=FAILED`` compare-and-swap the retire/reopen paths use,
    then transition the ticket to IGNORED when the FSM allows it so the reviewer ticket
    stops surfacing as active. No ``DeferredQuestion``: a closed PR is not a defect a
    human needs to triage.
    """
    reason = f"{task.execution_reason}\n{DEAD_REVIEW_STAMP}".strip() if task.execution_reason else DEAD_REVIEW_STAMP
    Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.COMPLETED,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
        execution_reason=reason,
    )
    _ignore_ticket_if_allowed(task.ticket)


def _ignore_ticket_if_allowed(ticket: Ticket) -> None:
    """Transition *ticket* to IGNORED when the FSM permits it; a no-op otherwise."""
    from django_fsm import can_proceed  # noqa: PLC0415 - deferred: FSM import at call time

    if not can_proceed(ticket.ignore):
        return
    ticket.ignore()
    ticket.save()


def _retire_superseded(task: Task) -> None:
    """Mark a SUPERSEDED FAILED task COMPLETED via CAS, stamping the reason. Idempotent.

    The ticket's FSM already reached this phase's output (``has_completed_phase``),
    so the dead FAILED row is retired instead of escalated — the same
    ``UPDATE ... WHERE status=FAILED`` compare-and-swap the reopen path uses, so a
    concurrent tick updates 0 rows and does not double-write. No FSM side effect and
    no ``DeferredQuestion``: the ticket's own state already answers the question.
    """
    reason = f"{task.execution_reason}\n{SUPERSEDED_STAMP}".strip() if task.execution_reason else SUPERSEDED_STAMP
    Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.COMPLETED,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
        execution_reason=reason,
    )


__all__ = [
    "DEAD_REVIEW_STAMP",
    "LIVE_SUCCESSOR_STAMP",
    "SUPERSEDED_STAMP",
    "dispose_without_reopen",
]
