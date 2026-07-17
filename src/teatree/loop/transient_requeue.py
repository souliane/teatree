"""Bounded auto-requeue of transient-FAILED tasks — the retry, hard-bounded.

``Task.fail()`` is terminal: a task that RETURNS a failure envelope (an outage,
a provisioning-step failure, an incomplete run, a coder yield that landed no
commit) lands FAILED and stays there forever — the crashed-session reclaim
(``reclaim_orphaned_claims``) only rescues expired-lease CLAIMED rows, never a
returned failure. This tick sweep reopens such a row (FAILED → PENDING) so the
next dispatch resumes it.

The retry is HARD-BOUNDED by the #2009 repair-loop budget so it can never retry
endlessly (it would always fail): a ticket-phase at its iteration cap, or stalled
on two consecutive identical failures, is NOT reopened and is escalated LOUDLY
via a durable :class:`DeferredQuestion` (§17.1 invariant 9) — never silently,
never forever.

A DETERMINISTIC failure (a test failure, an assertion, a schema/evidence refusal)
is NOT reopened blindly, but it must never sit silent either. On a non-terminal
ticket, a coding/debugging failure whose last attempt was an omitted-envelope
refusal (the coder emitted no trailing ``files_modified`` JSON) gets exactly ONE
bounded corrective retry — reopened with the emit-the-envelope instruction
appended to its prompt. Any other deterministic failure, and any envelope refusal
that already spent its one corrective retry, is escalated via the SAME
:class:`DeferredQuestion` path. An EMPTY-error FAILED task (no recorded error at
all — neither transient nor deterministic) would otherwise match no branch and
freeze silently; it is routed straight to the escalation path. The invariant: a
terminal FAILED task on a non-terminal ticket ALWAYS escalates or retries-once,
never freezes silently.

A once-escalated task is stamped (:data:`_HALT_STAMP` in ``execution_reason``) and
excluded from every subsequent scan, so the dead-letter set never grows the per-tick
work unboundedly and an answered/dismissed question can never resurrect a fresh
escalation for the same halted row.

Lives in ``teatree.loop`` (orchestration): it needs both the transient classifier
(``teatree.agents``) and the ``Task`` model (``teatree.core.models``), which sit
in the same ``domain`` layer and so cannot import each other — only an
orchestration-layer module may compose both.
"""

import logging

from teatree.agents.outage_classifier import is_transient_failure
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_repair import phase_attempts
from teatree.core.repair_loop import IterationStalled, MaxIterationsExceeded, requeue_verdict

logger = logging.getLogger(__name__)

_ESCALATION_MARKER = "[repair-halt task={pk}]"
#: Stamped onto ``execution_reason`` when a task is escalated (dead-lettered), so it
#: is excluded from every future scan — bounds per-tick work and makes the escalation
#: durably once-per-task regardless of whether the question is later answered.
_HALT_STAMP = "[repair-halt-parked]"

#: Phases whose omitted-envelope refusal earns the one-shot corrective retry.
_CORRECTIVE_PHASES = frozenset({"coding", "debugging"})
#: Idempotency stamp appended to ``execution_reason`` when the corrective retry
#: fires — its presence means the one retry was already spent (escalate next time).
_CORRECTIVE_MARKER = "[auto-corrective-retry]"
_CORRECTIVE_INSTRUCTION = (
    "your last run omitted the required trailing JSON result envelope with files_modified — emit it."
)
#: Error substrings that mark a malformed / missing result envelope (as opposed
#: to a genuine defect like an assertion or test failure).
_ENVELOPE_REFUSAL_MARKERS = (
    "missing required evidence",
    "unexpected keys",
    "result is not valid json",
    "result must be a json object",
)


def requeue_transient_failed() -> int:
    """Reopen transient-FAILED tasks within budget; corrective-retry-or-escalate the rest.

    Returns the count of tasks reopened (transient reopens + corrective retries).
    A terminal FAILED task on a non-terminal ticket is NEVER left silent: it is
    reopened, corrective-retried once, or escalated via ``DeferredQuestion`` — an
    empty-error task (no recorded error) escalates rather than freezing.

    The FAILED set is loaded ONCE with its attempts prefetched (no per-task N+1) and
    already-parked rows excluded, so the per-tick cost stays bounded as dead letters
    accumulate.
    """
    reopened = 0
    for task in _non_terminal_failed_tasks():
        error = _latest_error(task)
        if not error:
            # No recorded error → neither transient nor deterministic; must not freeze.
            _escalate_once(task, reason="failed with no recorded error")
        elif is_transient_failure(error):
            halt = _budget_halt_reason(task)
            if halt is None:
                reopened += _reopen(task)
            else:
                _escalate_once(task, reason=halt)
        else:
            reopened += _handle_deterministic(task)
    return reopened


def _handle_deterministic(task: Task) -> int:
    """Corrective-retry a spent-envelope coding failure once, else escalate. Returns the reopen count."""
    halt = _budget_halt_reason(task)
    if halt is not None:
        _escalate_once(task, reason=halt)
        return 0
    if _is_corrective_candidate(task):
        return _corrective_reopen(task)
    _escalate_once(task, reason=_latest_error(task) or "deterministic failure")
    return 0


def _is_corrective_candidate(task: Task) -> bool:
    """Whether *task* is an omitted-envelope coding refusal that has not yet been corrective-retried."""
    if normalize_phase(task.phase) not in _CORRECTIVE_PHASES:
        return False
    if _CORRECTIVE_MARKER in task.execution_reason:
        return False
    error = _latest_error(task).casefold()
    return any(marker in error for marker in _ENVELOPE_REFUSAL_MARKERS)


def _corrective_reopen(task: Task) -> int:
    """CAS FAILED → PENDING appending the emit-the-envelope instruction to the prompt.

    Uses the same conditional ``UPDATE ... WHERE status=FAILED`` compare-and-swap
    as :func:`_reopen` so a concurrent tick that already reopened the row updates 0
    rows and does not re-append the note.
    """
    note = f"{_CORRECTIVE_MARKER} {_CORRECTIVE_INSTRUCTION}"
    new_reason = f"{task.execution_reason}\n{note}".strip() if task.execution_reason else note
    return Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.PENDING,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
        execution_reason=new_reason,
    )


def _latest_error(task: Task) -> str:
    """The newest attempt's error, read from the prefetched ``attempts`` (no extra query)."""
    attempts = sorted(task.attempts.all(), key=lambda a: a.pk)  # ty: ignore[unresolved-attribute]  # Django reverse FK
    return attempts[-1].error if attempts else ""


def _non_terminal_failed_tasks() -> list[Task]:
    """FAILED tasks on a non-terminal ticket, minus already-parked rows, attempts prefetched.

    Excluding :data:`_HALT_STAMP`-stamped rows keeps the per-tick scan bounded as
    dead letters pile up (a monotonically growing FAILED set would otherwise degrade
    tick latency linearly); prefetching ``attempts`` removes the per-task N+1 that
    :func:`_latest_error` would otherwise issue for every FAILED row.
    """
    return list(
        Task.objects.filter(status=Task.Status.FAILED)
        .exclude(ticket__state__in=Ticket._TERMINAL_STATES)  # noqa: SLF001 — the model's SSOT terminal set
        .exclude(execution_reason__contains=_HALT_STAMP)
        .select_related("ticket")
        .prefetch_related("attempts"),
    )


def _budget_halt_reason(task: Task) -> str | None:
    """Return the loud halt reason if *task*'s phase is out of budget, else ``None`` (may requeue).

    Uses the pure :func:`~teatree.core.repair_loop.requeue_verdict` over the SAME
    recorded attempts the reclaim path budgets on, WITHOUT the escalation side
    effect of ``Task.check_requeue_allowed`` — this sweep escalates both the cap
    AND the stall itself (idempotently), which that helper does only for the
    stall.
    """
    attempts = phase_attempts(task)
    last_two = [a.error_fingerprint for a in attempts[-2:] if a.error_fingerprint]
    try:
        requeue_verdict(
            ticket_id=task.ticket.pk,
            phase=normalize_phase(task.phase),
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
        )
    except (MaxIterationsExceeded, IterationStalled) as exc:
        return str(exc)
    return None


def _reopen(task: Task) -> int:
    """CAS FAILED → PENDING; returns 1 on the winning transition, 0 if already moved.

    A single conditional ``UPDATE ... WHERE status=FAILED`` (the same
    backend-agnostic compare-and-swap ``reclaim_orphaned_claims`` uses) so a
    concurrent tick that already reopened the row updates 0 rows and does not
    double-dispatch.
    """
    return Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.PENDING,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
    )


def _stamp_halt(task: Task) -> None:
    """Park *task* out of future scans by stamping :data:`_HALT_STAMP` onto ``execution_reason``.

    Idempotent — a no-op once the stamp is present. This is the durable dedup: an
    escalated row is excluded from :func:`_non_terminal_failed_tasks`, so answering
    or dismissing the question can never resurrect a fresh escalation for it.
    """
    if _HALT_STAMP in task.execution_reason:
        return
    reason = f"{task.execution_reason}\n{_HALT_STAMP}".strip() if task.execution_reason else _HALT_STAMP
    Task.objects.filter(pk=task.pk).update(execution_reason=reason)


def _escalate_once(task: Task, *, reason: str) -> None:
    """Record a durable escalation for a halted task, exactly once per task, then park it.

    The row is stamped so it drops out of every future scan; the DeferredQuestion is
    deduped across ALL questions (answered or not) by a per-task marker, so a halted
    FAILED row can never spam the away-mode queue nor re-escalate once its question is
    answered/dismissed. Reuses the §17.1 invariant 9 surface (statusline /
    ``t3 teatree questions list`` / the Slack DM drain).
    """
    marker = _ESCALATION_MARKER.format(pk=task.pk)
    already = DeferredQuestion.objects.filter(question__contains=marker).exists()
    _stamp_halt(task)
    if already:
        return
    where = task.ticket.issue_url or f"ticket {task.ticket.pk}"
    question = (
        f"{marker} Auto-retry halted on {where} (phase {normalize_phase(task.phase)!r}): {reason} "
        "Re-queueing is stopped so it does not retry a doomed failure forever. "
        "How should it proceed — investigate, rework, or ignore?"
    )
    DeferredQuestion.record(question, session_id=str(task.session_id or ""))  # ty: ignore[unresolved-attribute]
