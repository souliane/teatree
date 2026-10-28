"""Headless task runner with checkout custody and harness dispatch."""

import asyncio
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from django.utils import timezone

from teatree.agents._runner_env import DispatchCredential, _provider_child_env, agent_spawn_env
from teatree.agents._runner_options import _build_options, _turn_ceiling  # noqa: F401 — compatibility re-exports
from teatree.agents.compaction_guard import GuardedHarness
from teatree.agents.credential_policy import spend_metered_key
from teatree.agents.dispatch_refusal import pre_harness_refusal
from teatree.agents.harness import Harness
from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness, route_hold
from teatree.agents.harness_registry import (
    HarnessFallbackError,
    HarnessFallbackKind,
    InvalidHarnessProviderError,
    NoAvailableHarnessError,
    UnknownHarnessError,
)
from teatree.agents.live_control import DEFAULT_IDLE_WINDOW_SECONDS
from teatree.agents.model_tiering import resolve_spawn_effort
from teatree.agents.own_pr_review_head import prepare_own_pr_review_head
from teatree.agents.phase_handoff import delivered_phase_handoff
from teatree.agents.pydantic_ai_resume import release_finished_thread, retain_run_thread
from teatree.agents.read_only_venue_brief import ReadOnlyBriefRefusedError
from teatree.agents.runner_cancel import settle_cancelled_checkout
from teatree.agents.runner_failure_taxonomy import limit_match as _limit_match  # noqa: F401 — compatibility re-export
from teatree.agents.runner_heartbeat import HEARTBEAT_INTERVAL_SECONDS as _HEARTBEAT_INTERVAL
from teatree.agents.runner_heartbeat import (
    HeartbeatRuntime,
    drain_reason_closing_connection,
    drive_with_heartbeat,
    renew_lease_closing_connection,
)
from teatree.agents.runner_interruption import CeilingSalvage, _record_failure
from teatree.agents.runner_outcomes import UNROUTED as _UNROUTED  # noqa: F401 — compatibility re-export
from teatree.agents.runner_outcomes import Transport as _Transport
from teatree.agents.runner_outcomes import outcome_failure as _outcome_failure  # noqa: F401 — compatibility re-export
from teatree.agents.runner_outcomes import record_outcome as _record_outcome
from teatree.agents.runner_outcomes import record_skill_assurance_attempt
from teatree.agents.runner_outcomes import record_success as _record_success  # noqa: F401 — compatibility re-export
from teatree.agents.runner_outcomes import resolve_dispatch_lane as _resolve_dispatch_lane
from teatree.agents.runner_preparation import Preflight as _Preflight
from teatree.agents.runner_preparation import PreparedRun as _PreparedRun
from teatree.agents.runner_preparation import (
    _active_agent_count,
    _admission_park_or_child_env,
    _capped_forge_env,
    _ChildEnvDispatch,
    _resolve_child_env_or_failure,
    credential_fallback_kind,
)
from teatree.agents.runner_preparation import prepare_run as _prepare_run_impl
from teatree.agents.runner_preparation import restore_unconsumed_resume_thread as _restore_unconsumed_resume_thread
from teatree.agents.runner_route_recording import NO_ROUTE_FALLBACK as _NO_ROUTE_FALLBACK
from teatree.agents.runner_route_recording import RouteFailureRecord as _RouteFailureRecord
from teatree.agents.runner_route_recording import RouteFallback as _RouteFallback
from teatree.agents.runner_route_recording import dispatch_provider_name as _dispatch_provider_name
from teatree.agents.runner_route_recording import fail_routed_crash as _fail_routed_crash
from teatree.agents.runner_route_recording import fallback_reason_for_outcome as _fallback_reason_for_outcome
from teatree.agents.runner_route_recording import learn_route_failure as _learn_route_failure
from teatree.agents.runner_route_recording import park_after_route_failure as _park_after_route_failure
from teatree.agents.runner_route_recording import park_until as _park_until
from teatree.agents.runner_route_recording import record_route_failure_attempt as _record_route_failure_attempt
from teatree.agents.runner_route_recording import selected_fallback_reason as _selected_fallback_reason
from teatree.agents.runner_skill_staging import park_if_worker_skill_roots_empty, staged_skills_or_refusal
from teatree.agents.runner_stream import HarnessOutcome, _collect  # noqa: F401 — compatibility re-export
from teatree.agents.runner_usage import DispatchProvenance, resolve_provenance_effort
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage, _sample_usage_closing_connection
from teatree.agents.skill_assurance import SkillDispatchError
from teatree.agents.skill_routing import (
    AmbiguousSkillRouteError,
    ConflictingHarnessRoutingError,
    runtime_fallback_reason,
)
from teatree.agents.spawn_payload import AgentSpawnError
from teatree.agents.usage_window import park_on_timed_rejections
from teatree.config.agent_spawn import InvalidAgentConfigError
from teatree.core.cleanup.process_table import nested_pid_namespace
from teatree.core.modelkit.db_retry import wait_out_locked_writes
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.task_claim import drive_claim
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.checkout_fence import CheckoutFenceError, park_refused_checkout, verify_task_checkout
from teatree.core.worktree.occupancy import WorktreeOccupiedError, occupy_ticket_checkout, task_holder_id
from teatree.llm.credentials import CredentialError
from teatree.types import SkillMetadata

logger = logging.getLogger(__name__)

# ``LoopWatchdog`` / ``TaskUsage`` moved to ``runner_watchdog`` but stay part of
# this module's public surface (overlay_sdk re-exports ``LoopWatchdog``; tests patch
# ``headless.TaskUsage.for_task`` / ``headless._sample_usage_closing_connection``).
__all__ = [
    "HarnessOutcome",
    "LoopWatchdog",
    "TaskUsage",
    "_active_agent_count",
    "_capped_forge_env",
    "_provider_child_env",
    "_resolve_backend_or_failure",
    "_resolve_child_env_or_failure",
    "run_agent",
    "shutil",
]

# Lease duration the heartbeat renews to. The renewal runs as an asyncio task on
# the SAME event loop the headless agent drives, so under CPU/event-loop
# starvation (a loaded box running several coders at once) the ``sleep`` between
# renewals stretches far past its nominal 60s. With the DB default 300s lease that
# is only ~5 heartbeats of slack: a starved worker misses a few renewals, its OWN
# ``reclaim_orphaned_claims`` scanner sees the lease expired and re-queues the task,
# and the still-running coder then aborts with "lease lost: re-claimed by another
# worker" — a self-inflicted reclaim, NOT a second executor. Renewing to 15x the
# heartbeat interval widens the slack to ~15 min of continuous starvation before a
# false lapse, while a genuinely dead session's task still reclaims after the wider window.
_LEASE_SECONDS = 15 * _HEARTBEAT_INTERVAL  # 900s


@dataclass(frozen=True, slots=True)
class _RouteRetry:
    task: Task
    phase: str
    overlay_skill_metadata: SkillMetadata
    handoff: Path | None
    dispatch: DispatchHarness
    account: str = ""

    def after(self, record: _RouteFailureRecord, *, learn_failure: bool = True) -> TaskAttempt:
        attempt = _record_route_failure_attempt(self.task, self.dispatch, record)
        _alert_owner_of_metered_fallback(attempt)
        side_effects = record.outcome is not None and bool(record.outcome.tool_calls)
        if record.outcome is not None:
            release_finished_thread(self.task)
        moment = timezone.now()
        retry_at = self._spent_key_retry_at(record, moment)
        if retry_at is None:
            if side_effects:
                _park_after_route_failure(self.task, self.dispatch, record, phase=self.phase)
                return attempt
        elif side_effects or not record.account or (retry_at > moment and not self.dispatch.has_later_candidate):
            _park_until(self.task, retry_at)
            return attempt
        if learn_failure and (retry_at is None or not record.account):
            _learn_route_failure(self.task, self.dispatch, record, phase=self.phase)
        return _run_agent(
            self.task,
            phase=self.phase,
            overlay_skill_metadata=self.overlay_skill_metadata,
            handoff=self.handoff,
            route_fallback=_RouteFallback(record.reason, int(attempt.pk)),
        )

    def _spent_key_retry_at(self, record: _RouteFailureRecord, moment: datetime) -> datetime | None:
        if not record.refused_for_credit:
            return None
        return spend_metered_key(
            scope=self.task.ticket.overlay or "", lane=record.lane, signer=record.account, moment=moment
        )


def _retry_route_exception(
    exc: CredentialError | AgentSpawnError | HarnessFallbackError,
    *,
    task: Task,
    preflight: "_Preflight",
    route_retry: _RouteRetry,
    route_fallback: _RouteFallback,
) -> TaskAttempt | None:
    if (
        isinstance(exc, HarnessFallbackError)
        and exc.kind is HarnessFallbackKind.QUOTA_EXHAUSTED
        and route_retry.phase in {"codex_reviewing", "codex_adversarial_reviewing"}
        and preflight.dispatch.name == "codex_app_server"
    ):
        from teatree.core.review.backend_cooldown import record_quota_exhaustion  # noqa: PLC0415 — ORM at call time

        try:
            record_quota_exhaustion(
                backend="codex",
                overlay=task.ticket.overlay or "",
                returncode=1,
                stderr=str(exc),
                failure_kind=exc.kind.value,
            )
        except Exception:
            logger.warning("Could not persist Codex review quota cooldown", exc_info=True)
    fallback = _RouteFallback(
        _selected_fallback_reason(route_fallback, preflight.dispatch),
        route_fallback.source_attempt_id,
    )
    record = _RouteFailureRecord(
        str(exc),
        preflight.skills,
        fallback=fallback,
        agent_session_id=exc.agent_session_id if isinstance(exc, HarnessFallbackError) else "",
        kind=(
            exc.kind
            if isinstance(exc, HarnessFallbackError)
            else credential_fallback_kind(exc)
            if isinstance(exc, CredentialError)
            else None
        ),
        account=route_retry.account,
    )
    if isinstance(exc, HarnessFallbackError) and exc.side_effects_started:
        attempt = _record_route_failure_attempt(task, preflight.dispatch, record)
        _park_after_route_failure(task, preflight.dispatch, record, phase=route_retry.phase)
        return attempt
    if (
        isinstance(exc, ReadOnlyBriefRefusedError)
        and preflight.dispatch.route_candidate_index is not None
        and not preflight.dispatch.has_later_candidate
    ):
        hold = route_hold(preflight.dispatch.name, str(exc))
        return park_on_timed_rejections(task, (hold,)) or _record_failure(  # no-usage: no billed turn ran
            task, error=str(hold)
        )
    if preflight.dispatch.route_candidate_index is None:
        return None
    if isinstance(exc, HarnessFallbackError) or runtime_fallback_reason(exc):
        return route_retry.after(record, learn_failure=not isinstance(exc, ReadOnlyBriefRefusedError))
    return None


def _prepare_run(
    task: Task,
    preflight: "_Preflight",
    *,
    phase: str,
    handoff: Path | None,
    credential: DispatchCredential,
) -> _PreparedRun:
    return _prepare_run_impl(
        task,
        preflight,
        phase=phase,
        handoff=handoff,
        credential=credential,
    )


def _or_route_retry[T](
    step: Callable[[], T],
    preflight: "_Preflight",
    route_retry: _RouteRetry,
    route_fallback: _RouteFallback,
) -> T | TaskAttempt:
    """Run a step before the harness opens; a typed fallback moves to the next route candidate."""
    task = route_retry.task
    try:
        return step()
    except HarnessFallbackError as exc:
        _restore_unconsumed_resume_thread(preflight.dispatch.harness)
        retry = _retry_route_exception(
            exc,
            task=task,
            preflight=preflight,
            route_retry=route_retry,
            route_fallback=route_fallback,
        )
        if retry is not None:
            return retry
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: refused before any turn was billed


def _ready_run_or_failure(
    preflight: "_Preflight",
    route_retry: _RouteRetry,
    route_fallback: _RouteFallback,
    lane: str,
) -> tuple[DispatchCredential, _PreparedRun] | TaskAttempt:
    dispatch = preflight.dispatch
    child_env = _ChildEnvDispatch(lane, route_retry.phase, dispatch.route_candidate_index is not None)
    credential = _or_route_retry(
        lambda: _admission_park_or_child_env(route_retry.task, dispatch.harness, dispatch.provider, context=child_env),
        preflight,
        route_retry,
        route_fallback,
    )
    if isinstance(credential, TaskAttempt):
        return credential
    try:
        prepared = _or_route_retry(
            lambda: _prepare_run(
                route_retry.task, preflight, phase=route_retry.phase, handoff=route_retry.handoff, credential=credential
            ),
            preflight,
            route_retry,
            route_fallback,
        )
    except SkillDispatchError as exc:
        _restore_unconsumed_resume_thread(preflight.dispatch.harness)
        logger.warning("Refusing task %s: %s", route_retry.task.pk, exc)
        attempt = park_if_worker_skill_roots_empty(route_retry.task, exc)
        if attempt is None:
            attempt = _record_failure(  # no-usage: required skill refused before the harness opened; no turn billed
                route_retry.task, error=str(exc), result={"skill_assurance": exc.assurance}
            )
        record_skill_assurance_attempt(route_retry.task, attempt)
        return attempt
    if isinstance(prepared, TaskAttempt):
        return prepared
    return credential, prepared


@wait_out_locked_writes()
def run_agent(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> TaskAttempt:
    """Hold checkout occupancy before driving the task claim and harness."""
    namespace = nested_pid_namespace()
    try:
        with occupy_ticket_checkout(
            task.ticket, holder=task_holder_id(task), holder_session=task.claimed_by_session, namespace=namespace
        ) as path:
            verify_task_checkout(task, path, namespace=namespace)
            with drive_claim(task), delivered_phase_handoff(task) as handoff:
                try:
                    attempt = _run_agent(
                        task, phase=phase, overlay_skill_metadata=overlay_skill_metadata, handoff=handoff
                    )
                    _alert_owner_of_metered_fallback(attempt)
                    return attempt
                finally:
                    if path:
                        settle_cancelled_checkout(task, Path(path))
    except (WorktreeOccupiedError, CheckoutFenceError) as exc:
        logger.warning("Parking dispatch for task %s: %s", task.pk, exc)
        return park_refused_checkout(task, exc)


def _alert_owner_of_metered_fallback(attempt: TaskAttempt) -> None:
    if attempt.lane != TaskAttempt.Lane.METERED or not attempt.route_candidate_index:
        return
    day = timezone.now().date().isoformat()
    try:
        notify_user(
            f"An agent route fell through to the metered lane on {day} UTC. "
            "Check the Metered group in check-in for estimated cost.",
            kind=NotifyKind.INFO,
            idempotency_key=f"agent_route_paid_fallback:{day}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.warning("owner alert for a metered route fallback failed — continuing", exc_info=True)


def _run_agent(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    handoff: Path | None = None,
    route_fallback: _RouteFallback = _NO_ROUTE_FALLBACK,
) -> TaskAttempt:
    """Drive an agent for *task* in-process via the ``agent_harness`` backend."""
    preflight = _preflight(task, phase=phase, overlay_skill_metadata=overlay_skill_metadata)
    if isinstance(preflight, TaskAttempt):
        return preflight
    harness = preflight.dispatch.harness
    route_retry = _RouteRetry(task, phase, overlay_skill_metadata, handoff, preflight.dispatch)

    # Read off the SAME selection that built the harness, so a probe answering differently on
    # a second pass can never split the transport from its credential.
    lane = _resolve_dispatch_lane(harness, preflight.dispatch.provider)

    ready = _ready_run_or_failure(preflight, route_retry, route_fallback, lane)
    if isinstance(ready, TaskAttempt):
        return ready
    credential, prepared = ready
    route_retry = replace(route_retry, account=credential.account)

    # Resolved HERE, not inside the coroutine (#3980): Django refuses a synchronous ORM read to a
    # thread that owns a running event loop, and the config resolver catches that refusal and
    # resolves the whole DB override tier as unreadable — so a ceiling read from inside
    # ``asyncio.run`` silently returns shipped defaults on every dispatch instead of failing.
    watchdog = LoopWatchdog.from_settings()
    try:
        with agent_spawn_env():
            outcome = asyncio.run(
                _drive_with_heartbeat(
                    task,
                    prepared.prompt,
                    prepared.options,
                    GuardedHarness(harness=harness, guard=prepared.compaction_guard, name=preflight.dispatch.name),
                    watchdog=watchdog,
                )
            )
    except (CredentialError, AgentSpawnError, HarnessFallbackError) as exc:
        # Two ways a run never STARTS, recorded identically as their own one-line cause.
        # A non-ClaudeSdkHarness resolves its own credential lazily inside
        # ``harness.open`` — this is the same "fail loud, record it" contract
        # the eager ``child_env`` catch above gives the ClaudeSdkHarness. An
        # ``AgentSpawnError`` is the child that could not be exec'd (#4301): recorded
        # here so the durable text is the named cause the repair-halt reads, not the
        # forty SDK frames the generic re-raise below would store.
        # ``resolve_dispatch_harness`` (above) already popped any resumed pydantic_ai
        # thread as a side effect of BUILDING the harness — restore it, since
        # a run that never opened never actually consumed it (#2916).
        _restore_unconsumed_resume_thread(harness)
        attempt = _retry_route_exception(
            exc,
            task=task,
            preflight=preflight,
            route_retry=route_retry,
            route_fallback=route_fallback,
        )
        if attempt is None:
            logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
            attempt = _record_failure(task, error=str(exc))  # no-usage: the harness never opened, so no turn was billed
        return attempt
    except Exception:
        # AH-3 / #2916: a NON-CredentialError ``open()`` (or drive) failure must not
        # irrecoverably destroy a resumed task's parked thread either. ``resolve_dispatch_harness``
        # popped it when BUILDING the harness, and an escaping exception yields no outcome,
        # so the retain below never runs. Restore it so the resumed conversation survives for
        # a retry. An unrouted failure then propagates to the caller (``tasks.py``), which records
        # the durable failed attempt with the full traceback; a routed one is recorded here.
        _restore_unconsumed_resume_thread(harness)
        if preflight.dispatch.route_candidate_index is None:
            raise
        return _fail_routed_crash(task, preflight.dispatch, preflight.skills)

    # Retained before any outcome write, since the requeue sweep can see the row from that write on.
    retain_run_thread(task, outcome.thread)
    route_failure = _fallback_reason_for_outcome(
        outcome,
        metered_transport=harness.capabilities.metered_lane,
        lane=lane,
    )
    if preflight.dispatch.route_candidate_index is not None and route_failure:
        return route_retry.after(
            _RouteFailureRecord(
                route_failure,
                preflight.skills,
                outcome,
                lane,
                _RouteFallback(
                    _selected_fallback_reason(route_fallback, preflight.dispatch),
                    route_fallback.source_attempt_id,
                ),
                account=credential.account,
            ),
        )
    # #3673 Tier 3 provenance: resolve the per-tier effort the same way
    # ``_build_options`` pins it on the spawn (a deterministic settings read, so
    # the two resolutions never diverge) and pair it with the resolved skill
    # bundle, so the recorded attempt carries exactly what this dispatch ran with.
    attempt = _record_outcome(
        task,
        replace(
            outcome,
            compaction_stopped=prepared.compaction_guard.stopped_run,
            resumed=bool(prepared.options.resume),
        ),
        harness,
        CeilingSalvage(
            phase=phase,
            lane=lane,
            account=credential.account,
            provenance=DispatchProvenance(
                reasoning_effort=preflight.dispatch.effort
                or resolve_provenance_effort(resolve_spawn_effort, phase, preflight.dispatch.name),
                skills_loaded=tuple(preflight.skills),
                skill_assurance=prepared.skill_assurance,
                selected_harness=preflight.dispatch.name,
                selected_provider=_dispatch_provider_name(preflight.dispatch),
                selected_model=preflight.dispatch.model or prepared.options.model or "",
                route_candidate_index=preflight.dispatch.route_candidate_index,
                route_source_skill=preflight.dispatch.route_source_skill,
                fallback_reason=_selected_fallback_reason(route_fallback, preflight.dispatch),
                fallback_from_attempt_id=route_fallback.source_attempt_id,
            ),
        ),
        transport=_Transport(account=credential.account, metered=harness.capabilities.metered_lane),
    )
    release_finished_thread(task)
    return attempt


def _preflight(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> _Preflight | TaskAttempt:
    """The refusals that must all pass before a turn can be billed, as one short-circuit."""
    if (held := prepare_own_pr_review_head(task, phase=phase)) is not None:
        return held
    refusal = pre_harness_refusal(task, phase=phase)
    if refusal is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, refusal)
        return _record_failure(task, error=refusal)  # no-usage: refused before the harness opened — no turn billed

    staged = staged_skills_or_refusal(task, phase=phase, overlay_skill_metadata=overlay_skill_metadata)
    if isinstance(staged, TaskAttempt):
        return staged
    stage_skills, skills = staged
    dispatch = _resolve_backend_or_failure(task, phase=phase, skills=skills)
    if isinstance(dispatch, TaskAttempt):
        return dispatch

    return _Preflight(stage_skills=stage_skills, dispatch=dispatch, skills=skills)


def _resolve_backend_or_failure(
    task: Task, *, phase: str = "", skills: list[str] | None = None
) -> DispatchHarness | TaskAttempt:
    """Resolve the headless transport ONCE, or a recorded failure for an unresolvable backend."""
    try:
        return resolve_dispatch_harness(task, phase=phase or None, skills=skills)
    except NoAvailableHarnessError as exc:
        parked = park_on_timed_rejections(task, exc.rejected)
        return parked or _record_failure(task, error=str(exc))  # no-usage: no candidate opened, so no turn billed
    except (
        NotImplementedError,
        UnknownHarnessError,
        InvalidHarnessProviderError,
        CredentialError,
        AmbiguousSkillRouteError,
        ConflictingHarnessRoutingError,
        InvalidAgentConfigError,
    ) as exc:
        return _record_failure(task, error=str(exc))  # no-usage: an unimplemented/unresolvable backend never ran


def _renew_lease_closing_connection(task: Task) -> None:
    renew_lease_closing_connection(task, lease_seconds=_LEASE_SECONDS)


async def _drive_with_heartbeat(
    task: Task,
    prompt: str,
    options: ClaudeAgentOptions,
    harness: Harness,
    *,
    watchdog: LoopWatchdog,
) -> HarnessOutcome:
    return await drive_with_heartbeat(
        task,
        prompt,
        options,
        harness,
        runtime=HeartbeatRuntime(
            watchdog=watchdog,
            heartbeat_interval=_HEARTBEAT_INTERVAL,
            sample_usage=_sample_usage_closing_connection,
            renew_lease=_renew_lease_closing_connection,
            drain_reason=drain_reason_closing_connection,
            idle_window_seconds=DEFAULT_IDLE_WINDOW_SECONDS,
        ),
    )
