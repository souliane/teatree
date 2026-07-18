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

An EXHAUSTION-killed FAILED task — one that died on a Claude usage-window limit (a
subscription 5h/weekly window or a transient rate limit, recorded ``<cause>: …`` by
``LimitMatch.as_reason``) — is NOT a defect and must not be escalated to a human as one.
While ``limit_autorecovery_enabled`` is ON, such a task is auto-requeued once its window
HORIZON has elapsed since the last failed attempt (the deterministic, probe-free twin of
``usage_window_recovery`` for tasks that ALREADY landed FAILED — a limit hit while the
flag was off, or on a non-parking lane); before the horizon it is left FAILED and
re-checked on a later tick, never escalated (a capacity dip is not a doomed failure).
API-credit exhaustion is excluded (no timed reset) and stays on the escalation path. With
the flag OFF the branch is inert — an exhaustion failure follows the deterministic path
exactly as before, so the flag-off behaviour is byte-identical.

A SUPERSEDED FAILED task — one whose phase output the ticket's FSM already reached
(``ticket.has_completed_phase``) — is NOT escalated at all: it is a dead artifact of
an earlier interrupted run while the ticket advanced on its own, so it is retired
COMPLETED silently. This is the fix for the redispatch flood on already-done tickets
(3366/3336/3352): the away-mode queue is never asked about a phase the ticket's own
state already answers.

A once-escalated task is stamped (:data:`_HALT_STAMP` in ``execution_reason``) and
excluded from every subsequent scan, so the dead-letter set never grows the per-tick
work unboundedly. The ``DeferredQuestion`` itself is deduped by a STABLE key —
``(ticket, phase, failure-fingerprint)``, NOT the task pk — so the fresh ``Task`` rows
a stuck phase mints each redispatch cycle collapse to ONE open question instead of one
per cycle (the observed 10-15x duplicate flood).

Lives in ``teatree.loop`` (orchestration): it needs both the transient classifier
(``teatree.agents``) and the ``Task`` model (``teatree.core.models``), which sit
in the same ``domain`` layer and so cannot import each other — only an
orchestration-layer module may compose both.
"""

import logging
from datetime import datetime

from django.utils import timezone

from teatree.agents.outage_classifier import is_transient_failure
from teatree.agents.usage_window import autorecovery_enabled
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_repair import phase_attempts
from teatree.core.repair_loop import (
    IterationStalled,
    MaxIterationsExceeded,
    requeue_verdict,
    terminal_reason_fingerprint,
)
from teatree.llm.anthropic_limits import LimitCause, recoverable_exhaustion_cause, window_horizon

logger = logging.getLogger(__name__)

#: Stamped onto ``execution_reason`` when a task is escalated (dead-lettered), so it
#: is excluded from every future scan — bounds per-tick work and makes the escalation
#: durably once-per-task regardless of whether the question is later answered.
_HALT_STAMP = "[repair-halt-parked]"
#: Stamped onto ``execution_reason`` when a SUPERSEDED FAILED task is retired (its
#: phase output the ticket's FSM already reached). It is marked COMPLETED and drops
#: out of the scan — the away-mode queue is never asked about a phase the ticket
#: already advanced past (the 3366/3336/3352 redispatch-loop root cause).
_SUPERSEDED_STAMP = "[superseded-retired]"

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
    now = timezone.now()
    autorecovery = autorecovery_enabled()
    reopened = 0
    for task in _non_terminal_failed_tasks():
        if task.ticket.has_completed_phase(task.phase):
            # SUPERSEDED: the ticket's FSM already reached this phase's output, so this
            # FAILED row is a dead artifact of an earlier interrupted run. Retire it
            # silently — never escalate a question the ticket's own state answers.
            _retire_superseded(task)
            continue
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
        elif (cause := recoverable_exhaustion_cause(error)) is not None and autorecovery:
            reopened += _requeue_on_window_reset(task, cause, now=now)
        else:
            reopened += _handle_deterministic(task)
    return reopened


def _requeue_on_window_reset(task: Task, cause: LimitCause, *, now: datetime) -> int:
    """Reopen an exhaustion-killed task once its window has reset; else leave it FAILED (#3407).

    A task that died on a subscription session/weekly or transient rate limit is
    window-recoverable: capacity RETURNS at a known horizon after the failure. Once
    ``window_horizon(cause)`` has elapsed since the last failed attempt, the task is
    reopened within the #2009 budget (an over-budget one is escalated LOUDLY, never
    retried forever). Before the horizon it is left FAILED and re-checked on a later tick
    — NOT escalated, because a capacity dip is not a doomed defect. Returns the reopen
    count (0 or 1).
    """
    horizon = window_horizon(cause)
    last = _latest_attempt(task)
    ended = last.ended_at if last is not None else None
    if horizon is None or ended is None or ended + horizon > now:
        return 0
    halt = _budget_halt_reason(task)
    if halt is not None:
        _escalate_once(task, reason=halt)
        return 0
    return _reopen(task)


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


def _latest_attempt(task: Task) -> TaskAttempt | None:
    """The newest attempt from the prefetched ``attempts`` (no extra query), or ``None``."""
    attempts = sorted(task.attempts.all(), key=lambda a: a.pk)  # ty: ignore[unresolved-attribute]  # Django reverse FK
    return attempts[-1] if attempts else None


def _latest_error(task: Task) -> str:
    """The newest attempt's error, read from the prefetched ``attempts`` (no extra query)."""
    attempt = _latest_attempt(task)
    return attempt.error if attempt is not None else ""


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

    Idempotent — a no-op once the stamp is present. The per-task park: an escalated
    row is excluded from :func:`_non_terminal_failed_tasks`, so answering or dismissing
    the question can never resurrect a fresh escalation for THIS row. Cross-row
    collapse of the fresh ``Task`` rows a stuck phase mints each cycle is the separate
    job of the stable ``dedupe_marker`` (:func:`_escalation_marker`).
    """
    if _HALT_STAMP in task.execution_reason:
        return
    reason = f"{task.execution_reason}\n{_HALT_STAMP}".strip() if task.execution_reason else _HALT_STAMP
    Task.objects.filter(pk=task.pk).update(execution_reason=reason)


def _retire_superseded(task: Task) -> None:
    """Mark a SUPERSEDED FAILED task COMPLETED via CAS, stamping the reason. Idempotent.

    The ticket's FSM already reached this phase's output (``has_completed_phase``),
    so the dead FAILED row is retired instead of escalated — the same
    ``UPDATE ... WHERE status=FAILED`` compare-and-swap the reopen path uses, so a
    concurrent tick updates 0 rows and does not double-write. No FSM side effect and
    no ``DeferredQuestion``: the ticket's own state already answers the question.
    """
    reason = f"{task.execution_reason}\n{_SUPERSEDED_STAMP}".strip() if task.execution_reason else _SUPERSEDED_STAMP
    Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.COMPLETED,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
        execution_reason=reason,
    )


def _escalation_marker(task: Task) -> str:
    """Stable dedupe key for a halted task's escalation — ``(ticket, phase, failure)``.

    Keyed on the ticket + canonical phase + the NORMALIZED failure fingerprint, NOT
    the task pk: a stuck phase mints a FRESH ``Task`` row every redispatch cycle, so a
    per-task key filed one identical question per cycle (the observed 10-15x flood).
    Keying on the underlying standing condition instead collapses every churned row
    that fails the same way to ONE open ``DeferredQuestion``. A genuinely different
    failure (different fingerprint) keeps its own key, so a real new problem still
    surfaces. Truncated to fit the indexed ``dedupe_marker`` column (max 64).
    """
    fingerprint = terminal_reason_fingerprint(_latest_error(task))
    phase = normalize_phase(task.phase)
    return f"repair-halt:{task.ticket_id}:{phase}:{fingerprint}"[:64]  # ty: ignore[unresolved-attribute]


def _escalate_once(task: Task, *, reason: str) -> None:
    """Record a durable escalation for a halted task, then park the row. Deduped by condition.

    The row is stamped (:data:`_HALT_STAMP`) so it drops out of every future scan; the
    ``DeferredQuestion`` is deduped by the STABLE :func:`_escalation_marker` (ticket +
    phase + failure fingerprint) through the model's indexed ``dedupe_marker`` — so the
    fresh ``Task`` rows a stuck phase mints each redispatch cycle collapse to a SINGLE
    open question instead of one per cycle. Reuses the §17.1 invariant 9 surface
    (statusline / ``t3 teatree questions list`` / the Slack DM drain).
    """
    _stamp_halt(task)
    where = task.ticket.issue_url or f"ticket {task.ticket.pk}"
    phase = normalize_phase(task.phase)
    question = (
        f"[repair-halt ticket={task.ticket.pk} phase={phase!r}] Auto-retry halted on {where}: {reason} "
        "Re-queueing is stopped so it does not retry a doomed failure forever. "
        "How should it proceed — investigate, rework, or ignore?"
    )
    DeferredQuestion.record(
        question,
        session_id=str(task.session_id or ""),  # ty: ignore[unresolved-attribute]
        dedupe_marker=_escalation_marker(task),
    )
