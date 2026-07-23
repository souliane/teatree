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

A SELF-CORRECTABLE FAILED task — one whose deterministic failure is a config breach
with exactly ONE valid resolution (an invalid ``agent_harness``/``agent_harness_provider``
pair) — is CORRECTED and reopened rather than escalated (#3665). The message was
excellent; the paging was the defect. Self-repair stays loud (a WARNING log and the
durable :data:`~teatree.core.config_self_repair.SELF_REPAIR_STAMP` the dashboard renders)
and fires at most once per task, so it cannot become the silent-failure bug class it
replaces. The criterion — exactly one valid resolution, never a guess between two —
lives in :mod:`teatree.core.config_self_repair`.

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

A FAILED task WITH A LIVE SUCCESSOR — a newer, still-active (PENDING/CLAIMED) sibling
Task on the same ``(ticket, phase)`` — is PARKED (left FAILED, stamped out of every
future scan), never escalated (3534). A stuck-phase redispatch mints a fresh Task and
can re-claim the predecessor's lease out from under it; the predecessor lands FAILED
carrying a ``stuck_loop: lease lost … re-claimed`` breach even though the phase is
recovering fine under the successor. Escalating it files a ``DeferredQuestion`` already
stale at write time — the successor has the work, so the only correct answer is
"ignore". The park deliberately does NOT mark the row COMPLETED: the phase has not
finished (the successor is still mid-flight), and a COMPLETED row would become the
ticket's newest completed task, so ``replay_orphaned_transitions`` would fire its phase
transition on the next tick and silently advance the ticket past a phase nobody landed.

A DEAD-REVIEW-TARGET FAILED task — a review/codex-review phase (``reviewing`` /
``codex_reviewing`` / ``codex_adversarial_reviewing`` / ``e2e_reviewing``) whose
linked PR is provably MERGED/CLOSED — is likewise retired COMPLETED (and its reviewer
ticket IGNORED), never reopened: a verdict can never land on a dead PR, so
re-dispatching only burns a session that re-confirms the close (3556). Fail-OPEN on an
UNKNOWN PR state so a transient forge hiccup never retires a live review.

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
from teatree.core.config_self_repair import SELF_REPAIR_STAMP
from teatree.core.modelkit.phase_tools import VERDICT_REVIEW_PHASES
from teatree.core.modelkit.phases import normalize_phase, phase_spellings
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
from teatree.loop.config_self_repair import repair_for_error

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
#: Stamped onto ``execution_reason`` when a FAILED task is parked because a newer,
#: still-active sibling Task holds its ``(ticket, phase)`` (#3534). The row stays
#: FAILED — the phase has not completed — and drops out of the scan, so the stale
#: predecessor neither escalates nor advances the ticket's FSM.
_LIVE_SUCCESSOR_STAMP = "[superseded-parked]"

#: Stamped onto ``execution_reason`` when a review/codex-review task is retired
#: because its linked PR is provably MERGED/CLOSED. A review verdict can never land
#: on a dead PR, so re-dispatching only burns a session that re-confirms the close
#: (#3556). The task is marked COMPLETED and the reviewer ticket is IGNORED so it
#: drops out of every active scan instead of re-dispatching indefinitely.
_DEAD_REVIEW_STAMP = "[dead-review-retired]"

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
        # Per-item fault isolation (#3441): a single poison row (a corrupt attempt, a
        # classifier blow-up, a scheduling error) must NOT abort the sweep and strand
        # every OTHER loop's FAILED tasks. Record the failure loudly and move on.
        try:
            reopened += _route_failed_task(task, now=now, autorecovery=autorecovery)
        except Exception:
            logger.exception(
                "Transient-requeue skipped task %s (ticket %s) after an unexpected error",
                task.pk,
                task.ticket_id,  # ty: ignore[unresolved-attribute]
            )
    return reopened


def _route_failed_task(task: Task, *, now: datetime, autorecovery: bool) -> int:
    """Route ONE FAILED task to reopen / dispose / corrective-retry / escalate. Returns the reopen count.

    Isolated per task so :func:`requeue_transient_failed` can wrap it in a single
    ``try`` and keep sweeping when one row raises — a terminal FAILED task on a
    non-terminal ticket is still never left silent (reopened, disposed, retried, or
    escalated), it just can no longer take the whole tick down with it.
    """
    if _dispose_without_reopen(task):
        return 0
    error = _latest_error(task)
    if not error:
        # No recorded error → neither transient nor deterministic; must not freeze.
        _escalate_once(task, reason="failed with no recorded error")
        return 0
    if is_transient_failure(error):
        halt = _budget_halt_reason(task)
        if halt is None:
            return _reopen(task)
        _escalate_once(task, reason=halt)
        return 0
    if (cause := recoverable_exhaustion_cause(error)) is not None and autorecovery:
        return _requeue_on_window_reset(task, cause, now=now)
    return _handle_deterministic(task)


def _requeue_on_window_reset(task: Task, cause: LimitCause, *, now: datetime) -> int:
    """Reopen an exhaustion-killed task once its window has reset; else leave it FAILED (#3407).

    A task that died on a subscription session/weekly or transient rate limit is
    window-recoverable: capacity RETURNS at a known horizon after the failure. Once
    ``window_horizon(cause)`` has elapsed since the last failed attempt, the task is
    reopened within the #2009 budget (an over-budget one is escalated LOUDLY, never
    retried forever). Before the horizon it is left FAILED and re-checked on a later tick
    — NOT escalated, because a capacity dip is not a doomed defect. Returns the reopen
    count (0 or 1).

    The horizon is anchored on the last attempt's ``ended_at`` when present, else on its
    ``started_at`` (#3444). A crashed / killed attempt can land FAILED having never
    recorded ``ended_at``; the old ``ended is None`` guard stranded such a task FOREVER
    (never past the horizon, never reopened, never escalated). ``started_at`` is
    ``auto_now_add`` so it is always set — anchoring on it makes the window elapse from a
    slightly earlier instant, which requeues the task rather than silently stranding it.
    """
    horizon = window_horizon(cause)
    last = _latest_attempt(task)
    if horizon is None or last is None:
        return 0
    anchor = last.ended_at or last.started_at
    if anchor + horizon > now:
        return 0
    halt = _budget_halt_reason(task)
    if halt is not None:
        _escalate_once(task, reason=halt)
        return 0
    return _reopen(task)


def _handle_deterministic(task: Task) -> int:
    """Self-repair, corrective-retry, or escalate a deterministic failure. Returns the reopen count."""
    if (repaired := _self_repair_reopen(task)) is not None:
        return repaired
    halt = _budget_halt_reason(task)
    if halt is not None:
        _escalate_once(task, reason=halt)
        return 0
    if _is_corrective_candidate(task):
        return _corrective_reopen(task)
    _escalate_once(task, reason=_latest_error(task) or "deterministic failure")
    return 0


def _self_repair_reopen(task: Task) -> int | None:
    """Correct a single-valid-resolution config breach and reopen *task*; ``None`` if it must page.

    The #3665 ruling: a repair-halt condition with exactly one valid resolution
    carries no decision, so it is corrected and logged rather than DM'd to the
    owner. Over-suppression is foreclosed on both sides — the correction is loud
    (WARNING log + the durable :data:`~teatree.loop.config_self_repair.SELF_REPAIR_STAMP`
    the dashboard's configuration band renders), and it fires at most ONCE per
    task, so a breach that survives its own repair escalates normally.
    """
    if SELF_REPAIR_STAMP in task.execution_reason:
        return None
    repair = repair_for_error(_latest_error(task))
    if repair is None:
        return None
    repair.apply()
    new_reason = f"{task.execution_reason}\n{repair.stamp()}".strip() if task.execution_reason else repair.stamp()
    return Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.PENDING,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
        execution_reason=new_reason,
    )


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

    Excluding the parked rows — :data:`_HALT_STAMP` (escalated) and
    :data:`_LIVE_SUCCESSOR_STAMP` (a live successor holds the phase) — keeps the
    per-tick scan bounded as dead letters pile up (a monotonically growing FAILED set
    would otherwise degrade tick latency linearly); prefetching ``attempts`` removes the
    per-task N+1 that :func:`_latest_error` would otherwise issue for every FAILED row.
    """
    return list(
        Task.objects.filter(status=Task.Status.FAILED)
        .exclude(ticket__state__in=Ticket._TERMINAL_STATES)  # noqa: SLF001 — the model's SSOT terminal set
        .exclude(execution_reason__contains=_HALT_STAMP)
        .exclude(execution_reason__contains=_LIVE_SUCCESSOR_STAMP)
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


def _dispose_without_reopen(task: Task) -> bool:
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

    * SUPERSEDED — the ticket's FSM already reached this phase's output, so the
        row is a leftover of an earlier interrupted run (the ticket advanced on its own).
    * DEAD REVIEW TARGET — a review/codex-review phase whose linked PR is
        merged/closed, so a verdict can never land; re-dispatching only burns a
        session that re-confirms the close (#3556).

    Both triggers are FSM-inert by construction — the ticket has already reached (or is
    being moved to) its terminal answer, so the COMPLETED row's replayed transition
    finds no matching guard. A live-successor row is NOT one of them: its phase is
    unfinished, so it is parked FAILED by :func:`_park_live_successor` instead.
    """
    if task.ticket.has_completed_phase(task.phase):
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
    The cost is that a parked row skips the :func:`_budget_halt_reason` check, which the
    escalation path would otherwise run. That is bounded: the park grants no retry (the
    row is never reopened), its ``TaskAttempt`` rows survive so ``phase_attempts`` still
    counts them against the phase budget, and the successor carries the redispatcher's
    own cap — so a doomed phase still hits its budget through the successor's own row.
    """
    return Task.objects.filter(
        ticket_id=task.ticket_id,  # ty: ignore[unresolved-attribute]
        phase__in=phase_spellings(normalize_phase(task.phase)),
        status__in=Task.Status.active(),
        pk__gt=task.pk,
    ).exists()


def _park_live_successor(task: Task) -> None:
    """Park a FAILED task whose ``(ticket, phase)`` a live successor now holds. Idempotent.

    Stamps :data:`_LIVE_SUCCESSOR_STAMP` so the row drops out of
    :func:`_non_terminal_failed_tasks` and is never escalated again, via the same
    ``UPDATE ... WHERE status=FAILED`` compare-and-swap the reopen/retire paths use.

    The row stays FAILED on purpose. Its phase did not finish — the successor is still
    mid-flight — so marking it COMPLETED (the ``_retire_superseded`` shape) would make it
    the ticket's newest completed task and ``Task.objects.replay_orphaned_transitions``
    would fire its phase transition on the next tick, advancing the ticket past work
    nobody landed (a PLANNED ticket to CODED, a TESTED one through ``review()``).
    """
    if _LIVE_SUCCESSOR_STAMP in task.execution_reason:
        return
    reason = (
        f"{task.execution_reason}\n{_LIVE_SUCCESSOR_STAMP}".strip() if task.execution_reason else _LIVE_SUCCESSOR_STAMP
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
    reason = f"{task.execution_reason}\n{_DEAD_REVIEW_STAMP}".strip() if task.execution_reason else _DEAD_REVIEW_STAMP
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
