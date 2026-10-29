"""Bounded auto-requeue of failed tasks.

The recovery table chooses transient retries, one corrective retry, or a halt.
Every reopen is bounded by the per-phase repair budget. A halted task keeps
its failure and a durable stamp; factory health reports it until the work
is superseded or the ticket settles. No owner question is created here.

A completed phase artifact, a live successor, a moved review target, a
cancelled task, and a timed usage-window reset retain their distinct disposal
paths. The sweep catches errors per task so one poison row cannot stop peers.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from functools import partial

from django.db import transaction
from django.utils import timezone

from teatree.agents.credential_policy import PLANS_ONLY_MARKER
from teatree.config import AgentHarnessProvider, get_effective_settings
from teatree.core.claim_liveness import RELEASED_CLAIM
from teatree.core.config_self_repair import SELF_REPAIR_STAMP
from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.managers_task_claim import redispatch_window
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import (
    HALT_STAMP,
    PLAN_STALE_PREFIX,
    FailureKind,
    RecoveryStrategy,
    classify_failure,
    recovery_strategy,
    stall_fingerprints,
)
from teatree.core.models import Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.review_target import (
    MOVED_REVIEW_RETRY_HEADS_KEY,
    REVIEW_RETRY_HEAD_KEY,
    review_target_for_task,
)
from teatree.core.models.task_repair_queries import adjacent_work_attempts, phase_attempts, ticket_phase_observations
from teatree.core.repair_loop import IterationStalled, MaxIterationsExceeded, requeue_verdict
from teatree.llm.anthropic_limits import LimitCause, recoverable_exhaustion_cause, window_horizon
from teatree.loop.config_self_repair import repair_for_error
from teatree.loop.transient_requeue_correction import CORRECTIVE_MARKER as _CORRECTIVE_MARKER
from teatree.loop.transient_requeue_correction import corrective_note, retry_review_contract, wrong_head_retry_head
from teatree.loop.transient_requeue_critic import critic_dispatch_halt, park_if_critic_settled
from teatree.loop.transient_requeue_disposal import (
    LIVE_SUCCESSOR_STAMP,
    MOVED_TARGET_KINDS,
    SUPERSEDED_HEAD_STAMP,
    park_if_live_successor,
    park_moved_target,
    retire_if_dead_artifact,
    stale_plan_halt_reason,
)
from teatree.loop.transient_requeue_halt import (
    _escalate_once,
    _is_review_anchor,
    _latest_attempt,
    _latest_error,
    _stamp_halt,
)
from teatree.loop.transient_requeue_halt import escalation_marker as _escalation_marker

logger = logging.getLogger(__name__)

escalation_marker = _escalation_marker


def requeue_transient_failed() -> int:
    """Reopen eligible failures and stamp the halted ones for factory health."""
    now = timezone.now()
    reopened = 0
    for task in _non_terminal_failed_tasks():
        # Per-item fault isolation (#3441): a single poison row (a corrupt attempt, a
        # classifier blow-up, a scheduling error) must NOT abort the sweep and strand
        # every OTHER loop's FAILED tasks. Record the failure loudly and move on.
        try:
            with redispatch_window() as refusal:
                if refusal:
                    continue
                reopened += _route_failed_task(task, now=now)
        except Exception:
            logger.exception(
                "Transient-requeue skipped task %s (ticket %s) after an unexpected error",
                task.pk,
                task.ticket_id,
            )
    return reopened


def _route_failed_task(task: Task, *, now: datetime) -> int:
    """Route one failed task to recovery or a recorded halt; return the reopen count.

    Isolated per task so :func:`requeue_transient_failed` can wrap it in a single
    ``try`` and keep sweeping when one row raises — a terminal FAILED task on a
    non-terminal ticket is still recorded (reopened, disposed, retried, or
    halted), and one row cannot take the whole tick down with it.
    """
    error = _latest_error(task)
    if (
        retire_if_dead_artifact(task)
        or _answer_moved_target(task, error=error)
        or park_if_live_successor(task)
        or park_if_critic_settled(task, error=error)
        or _park_cancelled(task, error=error)
    ):
        task.ticket.pop_task_thread(int(task.pk))
        return 0
    if not error:
        # No recorded error → neither transient nor deterministic; must not freeze.
        _escalate_once(task, reason="failed with no recorded error")
        return 0
    review_credential_retry = (
        _is_review_anchor(task)
        and classify_failure(error) in {FailureKind.CREDENTIAL_EXHAUSTED, FailureKind.CREDENTIAL_MISSING}
        and PLANS_ONLY_MARKER not in task.execution_reason
        and get_effective_settings(task.ticket.overlay or None).agent_harness_provider
        in {None, AgentHarnessProvider.SUBSCRIPTION_THEN_API_KEY}
    )
    # The kind is re-derived rather than read off the attempt, so a row classified by an older
    # build cannot route on a stale kind.
    strategy = recovery_strategy(classify_failure(error))

    if review_credential_retry:
        return _reopen(task, plans_only=True)

    if strategy is RecoveryStrategy.RETRY:
        return _retry_failed_task(task)

    if (cause := recoverable_exhaustion_cause(error)) is not None:
        return _requeue_on_window_reset(task, cause, now=now)
    return _handle_deterministic(task, strategy)


def _retry_failed_task(task: Task) -> int:
    if not Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).exists():
        return 0
    halt = critic_dispatch_halt(task, fallback=_budget_halt_reason)
    if halt is None:
        return _reopen(task)
    _escalate_once(task, reason=halt)
    return 0


def _answer_moved_target(task: Task, *, error: str) -> bool:
    """Park a run whose target moved under it, first queuing the recovery nothing else arms; ``True`` if handled.

    A stale plan is owed a planning pass and an author ticket's own review of a PR that moved is
    owed a fresh review; no sweep arms either, least of all for a ticket with an open PR. A
    reviewer ticket's moved head is re-armed by the PR sweep, so it is only parked. The recovery
    uses the work budget or the stale-plan cycle bound; an exhausted one halts internally.
    """
    kind = classify_failure(error)
    if kind not in MOVED_TARGET_KINDS:
        return False
    recovery = _moved_target_recovery(task, kind=kind, error=error)
    if recovery is None:
        park_moved_target(task)
        return True
    halt = _budget_halt_reason(task)
    if halt is None and kind == FailureKind.PLAN_STALE:
        halt = stale_plan_halt_reason(task)
    if halt is None:
        try:
            recovery()
        except InvalidTransitionError as exc:
            halt = f"could not queue the recovery for a target that moved: {exc}"
    if halt is None:
        park_moved_target(task)
    else:
        _escalate_once(task, reason=halt)
    return True


def _park_cancelled(task: Task, *, error: str) -> bool:
    """An operator's cancel is a decision already made, so the row is parked and nobody is asked about it."""
    if classify_failure(error) != FailureKind.CANCELLED:
        return False
    _stamp_halt(task)
    return True


def _moved_target_recovery(task: Task, *, kind: str, error: str) -> Callable[[], Task] | None:
    ticket = task.ticket
    if kind == FailureKind.PLAN_STALE:
        return partial(ticket.schedule_plan_reaffirm, refusal=error.removeprefix(PLAN_STALE_PREFIX), parent_task=task)
    target = review_target_for_task(task)
    if target is not None and target.bind_live_head:
        return partial(ticket.schedule_review, parent_task=task)
    return None


def _requeue_on_window_reset(task: Task, cause: LimitCause, *, now: datetime) -> int:
    """Reopen an exhaustion-killed task once its window has reset; else leave it FAILED (#3407).

    A task that died on a subscription session/weekly or transient rate limit is
    window-recoverable: capacity RETURNS at a known horizon after the failure. Once
    ``window_horizon(cause)`` has elapsed since the last failed attempt, the task is
    reopened within the #2009 budget (an over-budget one is halted with a health signal, never
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


def _handle_deterministic(task: Task, strategy: RecoveryStrategy) -> int:
    """Self-repair, corrective-retry, or halt a non-retried failure. Return the reopen count.

    The table says whether a bounded correction MAY apply to this kind; the two correction seams
    keep their own eligibility predicates (one valid resolution, one spent retry, the phase gate),
    and a correction they decline still records a halt rather than freezing.
    """
    correctable = strategy is RecoveryStrategy.CORRECTIVE_RETRY
    if correctable:
        repaired = _self_repair_reopen(task)
        if repaired is not None:
            return repaired
    halt = _budget_halt_reason(task)
    if halt is not None:
        _escalate_once(task, reason=halt)
        return 0
    if correctable:
        note = _corrective_note(task)
        if note is not None:
            return _corrective_reopen(task, note)
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
        session_continuation=task.continuation_on_requeue(),
        **RELEASED_CLAIM,
        execution_reason=new_reason,
    )


def _corrective_note(task: Task) -> str | None:
    error = _latest_error(task)
    if head := wrong_head_retry_head(error):
        dispatch = task.auto_review_dispatches.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
        retried_heads = task.ticket.extra.get(MOVED_REVIEW_RETRY_HEADS_KEY, {})
        if (
            dispatch is not None
            and dispatch.head_sha == head
            and isinstance(retried_heads, dict)
            and head in retried_heads
        ):
            return None
    return corrective_note(task, error)


def _corrective_reopen(task: Task, note: str) -> int:
    """CAS FAILED → PENDING appending the emit-the-envelope instruction to the prompt.

    Uses the same conditional ``UPDATE ... WHERE status=FAILED`` compare-and-swap
    as :func:`_reopen` so a concurrent tick that already reopened the row updates 0
    rows and does not re-append the note.
    """
    stamped = f"{_CORRECTIVE_MARKER} {note}"
    head = wrong_head_retry_head(_latest_error(task))
    reason = retry_review_contract(task, head) if head else task.execution_reason
    new_reason = f"{reason}\n{stamped}".strip() if reason else stamped
    with transaction.atomic():
        reopened = Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
            status=Task.Status.PENDING,
            session_continuation=task.continuation_on_requeue(),
            **RELEASED_CLAIM,
            execution_reason=new_reason,
        )
        if reopened and head:
            task.ticket.merge_extra(merge_into_dicts={REVIEW_RETRY_HEAD_KEY: {str(task.pk): head}})
    return reopened


def _non_terminal_failed_tasks() -> list[Task]:
    """FAILED tasks on a non-terminal ticket, minus already-parked rows, attempts prefetched.

    Excluding the parked rows — :data:`HALT_STAMP` (escalated) and
    :data:`~teatree.loop.transient_requeue_disposal.LIVE_SUCCESSOR_STAMP` (a live successor holds the
    phase) and :data:`~teatree.loop.transient_requeue_disposal.SUPERSEDED_HEAD_STAMP` (the PR moved past
    the reviewed head) — keeps the
    per-tick scan bounded as dead letters pile up (a monotonically growing FAILED set
    would otherwise degrade tick latency linearly); prefetching ``attempts`` removes the
    per-task N+1 that :func:`_latest_error` would otherwise issue for every FAILED row.
    A kept third-party claim waits out its Task lease: disposing it would free a live holder's checkout.
    """
    return list(
        Task.objects.filter(status=Task.Status.FAILED)
        .exclude(ticket__state__in=Ticket._SETTLED_STATES)  # noqa: SLF001 — the model's SSOT terminal set
        .exclude(execution_reason__contains=HALT_STAMP)
        .exclude(execution_reason__contains=LIVE_SUCCESSOR_STAMP)
        .exclude(execution_reason__contains=SUPERSEDED_HEAD_STAMP)
        .without_kept_claims()
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

    A CAUSELESS attempt is dropped from the stall comparison (#4075): this sweep's own
    corrective retry is what produces the second no-envelope attempt, so counting it made
    the halt an artifact of the repair rather than an observation about the work.

    The CAP is skipped for a synthetic cadence-anchor ticket (``architectural_review``,
    ``eval_local``, …) — same exemption and rationale as
    :func:`~teatree.core.models.task_repair.check_requeue_allowed`: ``PhaseCadence``
    re-fires such a ticket's phase forever by design, so a lifetime attempt count is not
    a doom signal there. Stall detection stays live either way.
    """
    attempts = phase_attempts(task)
    adjacent = adjacent_work_attempts(attempts, ticket_phase_observations(task.ticket_id, task.phase))
    last_two = stall_fingerprints((a.failure_kind, a.error_fingerprint) for a in adjacent)
    try:
        requeue_verdict(
            ticket_id=task.ticket.pk,
            phase=normalize_phase(task.phase),
            iteration_count=len(attempts),
            last_two_fingerprints=last_two,
        )
    except IterationStalled as exc:
        return str(exc)
    except MaxIterationsExceeded as exc:
        if is_synthetic_ticket_url(task.ticket.issue_url):
            return None
        return str(exc)
    return None


def _reopen(task: Task, *, plans_only: bool = False) -> int:
    """CAS FAILED → PENDING; returns 1 on the winning transition, 0 if already moved.

    A single conditional ``UPDATE ... WHERE status=FAILED`` (the same
    backend-agnostic compare-and-swap ``reclaim_orphaned_claims`` uses) so a
    concurrent tick that already reopened the row updates 0 rows and does not
    double-dispatch.

    The retry runs on the SAME row, whose ``parent_task`` is unchanged, so the
    conversation it continues is stamped here rather than inferred from that chain.
    """
    return Task.objects.filter(pk=task.pk, status=Task.Status.FAILED).update(
        status=Task.Status.PENDING,
        session_continuation=task.continuation_on_requeue(),
        **RELEASED_CLAIM,
        **({"execution_reason": f"{task.execution_reason}\n{PLANS_ONLY_MARKER}".strip()} if plans_only else {}),
    )
