"""What a headless run records when something INTERRUPTED it, rather than the work failing.

Split out of :mod:`teatree.agents.runner` (at its module-health LOC cap): deciding whether a
lost lease, a watchdog breach or a race with a rival worker is a failure, a no-op or a landed
outcome is one self-contained judgement, distinct from the run/heartbeat orchestration the
runner owns. ``_record_failure`` comes with them because it is the branch every one of those
judgements can fall through to; the runner imports it back for its own pre-turn failures.
"""

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.claim_liveness import RELEASED_CLAIM
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.phase_landing import phase_landing_evidence

if TYPE_CHECKING:
    from teatree.agents.attempt_recorder import AttemptUsage
    from teatree.agents.runner import HarnessOutcome

logger = logging.getLogger(__name__)

_STUCK_LOOP_PREFIX = "stuck_loop: "

#: Stamped into a no-op recovery attempt's ``result['summary']`` (#4834 periodic review).
#: Public so a consumer can tell "the claim CAS held — a rival's row was already done"
#: apart from a genuine second success: :func:`_record_noop_over_completed_row` never
#: drives new work, so counting it as a SUCCESS toward "this task ran twice" is exactly
#: the false alarm ``teatree.cli.doctor.checks_reconciliation._check_duplicate_execution``
#: exists to catch, not an instance of it.
NOOP_OVER_COMPLETED_MARKER = "the row had already completed — "


def _record_stuck_outcome(
    task: Task, outcome: "HarnessOutcome", *, stuck_reason: str, usage: "AttemptUsage | None" = None
) -> TaskAttempt:
    """Record an interrupted run: the LANDED outcome where its evidence exists, else the failure.

    An interruption noticed AFTER the row reached COMPLETED is a no-op, never a failure
    (#4100): the run had nothing left to hand over, and writing a failure over a finished
    row buries a real completion, inflates the environmental-failure rate and feeds the
    auto-repair sweep a "re-do this" signal for work that is done.

    Short of that, only a LOST LEASE qualifies for the landed outcome (#3982) — it says the
    lease lapsed, not that the work failed. A watchdog runtime/turns breach is a genuine
    runaway with no such alibi, so it stays a recorded failure however far the ticket has
    advanced.
    """
    if Task.objects.filter(pk=task.pk, status=Task.Status.COMPLETED).exists():
        return _record_noop_over_completed_row(
            task, interruption=stuck_reason, produced=_produced_result(outcome), usage=usage
        )
    cancelled = Task.objects.filter(pk=task.pk, status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED).first()
    if cancelled is not None:
        return _record_noop_over_cancelled_row(cancelled, interruption=stuck_reason, usage=usage)
    evidence = phase_landing_evidence(task, trust_phase_artifact=True) if outcome.lease_lost else ""
    if evidence:
        return _record_landed(task, evidence=evidence, lease_loss=stuck_reason, usage=usage)
    return _record_failure(task, error=f"{_STUCK_LOOP_PREFIX}{stuck_reason}", usage=usage)


def _produced_result(outcome: "HarnessOutcome") -> AgentResultBlob:
    """The envelope the interrupted run had already emitted, or ``{}`` if it emitted none (#4464)."""
    from teatree.agents.runner_result import parse_result  # noqa: PLC0415 — deferred: call-time import

    return parse_result(outcome.agent_text)


def _record_noop_over_completed_row(
    task: Task,
    *,
    interruption: str,
    produced: AgentResultBlob | None = None,
    usage: "AttemptUsage | None" = None,
) -> TaskAttempt:
    """Record the interruption of a run whose row had already COMPLETED — exit-0, no failure.

    The row's own status is left exactly as it is: this run is the one that has nothing to say
    about it. What it DID produce is another matter (#4464) — a reviewing agent that emitted
    its verdict envelope and only then lost its lease had that envelope dropped on the floor
    here, leaving a ``completed`` row with nothing persisted behind it, indistinguishable from
    a reviewer that silently declined to record. So *produced* rides onto the attempt, which
    makes the work durable and inspectable without re-driving an FSM the rival run owns.
    """
    logger.info("Task %s was interrupted after its row completed: %s", task.pk, interruption)
    return _record_interrupted_attempt(
        task, summary=f"{NOOP_OVER_COMPLETED_MARKER}{interruption}", produced=produced, usage=usage
    )


def _record_noop_over_cancelled_row(
    task: Task, *, interruption: str, usage: "AttemptUsage | None" = None
) -> TaskAttempt:
    """Record the interruption of a run an operator had already cancelled, leaving the cancel standing.

    Re-failing the row would reclassify it ``lease_lost``, which the stuck-ticket sweep reads
    as a genuine failure and re-dispatches (#4834).
    """
    from teatree.agents.attempt_recorder import usage_fields  # noqa: PLC0415 — deferred: call-time import

    logger.info("Task %s was interrupted after an operator cancelled it: %s", task.pk, interruption)
    return TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=1,
        error=f"{task.failure_reason} (run interrupted: {interruption})",
        result={},
        **usage_fields(usage),
    )


def _record_landed(task: Task, *, evidence: str, lease_loss: str, usage: "AttemptUsage | None" = None) -> TaskAttempt:
    """Record the outcome *task*'s own phase evidence supports, not the lost lease (#3982).

    A lost lease says the LEASE lapsed; it says nothing about the work. When the phase's
    output demonstrably landed, recording ``failed`` / ``lease_lost`` feeds the auto-repair
    sweep a "re-do this" signal for completed work and inflates the environmental-failure
    rate. The attempt is recorded exit-0 carrying both the evidence and the lease loss, so
    the interruption stays visible without being the verdict.

    The row is landed COMPLETED only while NOTHING holds it — a conditional
    ``UPDATE ... WHERE status=PENDING``, the same compare-and-swap
    ``transient_requeue_disposal._retire_superseded`` uses. A live successor's claim is therefore
    never terminated out from under it, and a row nobody took up after the in-process
    reclaim stops being re-dispatched for work that already shipped. No FSM side effect is
    needed: the evidence IS that the ticket already reached this phase's target state.
    """
    attempt = _record_interrupted_attempt(
        task, summary=f"phase landed despite a lost lease — {evidence}; {lease_loss}", usage=usage
    )
    Task.objects.filter(pk=task.pk, status=Task.Status.PENDING).update(status=Task.Status.COMPLETED, **RELEASED_CLAIM)
    logger.warning("Task %s lost its lease but its phase landed: %s", task.pk, evidence)
    return attempt


def _record_interrupted_attempt(
    task: Task,
    *,
    summary: str,
    produced: AgentResultBlob | None = None,
    usage: "AttemptUsage | None" = None,
) -> TaskAttempt:
    """The exit-0 attempt an interruption records when it is not the verdict on the work."""
    from teatree.agents.attempt_recorder import usage_fields  # noqa: PLC0415 — deferred: call-time import

    result: AgentResultBlob = {**(produced or {})}
    # The interruption is appended rather than substituted: both what the run produced and
    # what stopped it are load-bearing for whoever reads the attempt back.
    produced_summary = str(result.get("summary") or "").strip()
    result["summary"] = f"{produced_summary} — {summary}" if produced_summary else summary
    return TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=0,
        error="",
        result=result,
        **usage_fields(usage),
    )


def _record_failure(
    task: Task,
    *,
    exit_code: int = 1,
    error: str = "",
    result: AgentResultBlob | None = None,
    usage: "AttemptUsage | None" = None,
) -> TaskAttempt:
    """Record a FAILED attempt carrying *error* and whatever spend it billed, and fail the task.

    ``usage`` is ``None`` only where the failure happened BEFORE any turn was billed, which
    keeps the spend columns NULL rather than zero (#4164) — see :func:`usage_fields`.
    """
    from teatree.agents.attempt_recorder import usage_fields  # noqa: PLC0415 — deferred: call-time import

    attempt = TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=exit_code,
        error=error,
        result=result or {},
        **usage_fields(usage),
    )
    task.fail(reason=error)
    return attempt
