"""Headless agent runner — executes tasks without a terminal.

Drives an in-process agent behind the :class:`~teatree.agents.harness.Harness` seam and builds a real-environment
:class:`~claude_agent_sdk.ClaudeAgentOptions`, opens a session via the harness
backend selected by ``agent_harness`` (default: the ``claude-agent-sdk``
``ClaudeSDKClient``), captures the typed messages it yields, and stores the
result in ``TaskAttempt.result``. Unlike the clean-room eval runner
(``teatree.eval.api_runner``), this path runs a REAL task: it keeps the
developer's environment, skills, and context — no isolation, no
``setting_sources=[]``.

Wires only to ``Task`` / ``TaskAttempt`` models — no dashboard, no
process registry, no platform autostart.
"""

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from django.utils import timezone

from teatree.agents._runner_env import (
    DispatchCredential,
    _overlay_scope,
    _provider_child_env,
    agent_spawn_env,
    with_routed_github_token,
    with_test_worker_cap,
)
from teatree.agents._runner_options import _build_options, _turn_ceiling  # noqa: F401 — compatibility re-exports
from teatree.agents.compaction_guard import GuardedHarness
from teatree.agents.dispatch_refusal import pre_harness_refusal
from teatree.agents.harness import Harness
from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.agents.harness_registry import (
    HarnessFallbackError,
    HarnessFallbackKind,
    InvalidHarnessProviderError,
    UnknownHarnessError,
)
from teatree.agents.model_tiering import resolve_spawn_effort
from teatree.agents.phase_handoff import delivered_phase_handoff
from teatree.agents.pydantic_ai_resume import release_finished_thread, retain_run_thread
from teatree.agents.reader_profile import is_reader_phase, reader_child_env, reader_env_hermetic
from teatree.agents.runner_budget import TicketBudget
from teatree.agents.runner_failure_taxonomy import limit_match as _limit_match  # noqa: F401 — compatibility re-export
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat, renew_lease_closing_connection
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
from teatree.agents.runner_preparation import prepare_run as _prepare_run_impl
from teatree.agents.runner_route_recording import NO_ROUTE_FALLBACK as _NO_ROUTE_FALLBACK
from teatree.agents.runner_route_recording import RouteFailureRecord as _RouteFailureRecord
from teatree.agents.runner_route_recording import RouteFallback as _RouteFallback
from teatree.agents.runner_route_recording import dispatch_provider_name as _dispatch_provider_name
from teatree.agents.runner_route_recording import fallback_reason_for_outcome as _fallback_reason_for_outcome
from teatree.agents.runner_route_recording import learn_route_failure as _learn_route_failure
from teatree.agents.runner_route_recording import record_route_failure_attempt as _record_route_failure_attempt
from teatree.agents.runner_route_recording import selected_fallback_reason as _selected_fallback_reason
from teatree.agents.runner_stream import HarnessOutcome, _collect  # noqa: F401 — compatibility re-export
from teatree.agents.runner_usage import DispatchProvenance, resolve_provenance_effort
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage, _sample_usage_closing_connection
from teatree.agents.skill_assurance import SkillDispatchError
from teatree.agents.skill_bundle import (
    ArchitecturalReviewSkillMissingError,
    resolve_skill_bundle,
    stage_skills_for_dispatch,
)
from teatree.agents.skill_routing import (
    AmbiguousSkillRouteError,
    ConflictingHarnessRoutingError,
    runtime_fallback_reason,
)
from teatree.agents.spawn_payload import AgentSpawnError
from teatree.agents.usage_window import maybe_park_for_active_window, park_task_on_all_exhausted
from teatree.config import AgentHarnessProvider
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.task_claim import drive_claim
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.worktree.occupancy import WorktreeOccupiedError, occupy_ticket_checkout, task_holder_id
from teatree.credential_config import AllTokensExhaustedError
from teatree.llm.credentials import CredentialError
from teatree.types import SkillMetadata

logger = logging.getLogger(__name__)

# ``LoopWatchdog`` / ``TaskUsage`` moved to ``runner_watchdog`` but stay part of
# this module's public surface (overlay_sdk re-exports ``LoopWatchdog``; tests patch
# ``headless.TaskUsage.for_task`` / ``headless._sample_usage_closing_connection``).
__all__ = ["HarnessOutcome", "LoopWatchdog", "TaskUsage", "run_agent"]

_HEARTBEAT_INTERVAL = 60  # seconds

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
class _ChildEnvDispatch:
    lane: str = ""
    phase: str = ""
    route_candidate: bool = False


_DEFAULT_CHILD_ENV_DISPATCH = _ChildEnvDispatch()


@dataclass(frozen=True, slots=True)
class _RouteRetry:
    task: Task
    phase: str
    overlay_skill_metadata: SkillMetadata
    handoff: Path | None
    dispatch: DispatchHarness

    def after(self, record: _RouteFailureRecord) -> TaskAttempt:
        attempt = _record_route_failure_attempt(self.task, self.dispatch, record)
        if record.outcome is not None:
            release_finished_thread(self.task)
            if record.outcome.tool_calls:
                self.task.park(not_before=timezone.now())
                return attempt
        _learn_route_failure(self.task, self.dispatch, record.reason, phase=self.phase)
        return _run_agent(
            self.task,
            phase=self.phase,
            overlay_skill_metadata=self.overlay_skill_metadata,
            handoff=self.handoff,
            route_fallback=_RouteFallback(record.reason, int(attempt.pk)),
        )


def _retry_route_exception(
    exc: CredentialError | AgentSpawnError | HarnessFallbackError,
    *,
    task: Task,
    preflight: "_Preflight",
    route_retry: _RouteRetry,
    route_fallback: _RouteFallback,
) -> TaskAttempt | None:
    fallback = _RouteFallback(
        _selected_fallback_reason(route_fallback, preflight.dispatch),
        route_fallback.source_attempt_id,
    )
    record = _RouteFailureRecord(
        str(exc),
        preflight.skills,
        fallback=fallback,
        agent_session_id=exc.agent_session_id if isinstance(exc, HarnessFallbackError) else "",
    )
    if isinstance(exc, HarnessFallbackError) and exc.side_effects_started:
        attempt = _record_route_failure_attempt(task, preflight.dispatch, record)
        task.park(not_before=timezone.now())
        return attempt
    if preflight.dispatch.route_candidate_index is None:
        return None
    if isinstance(exc, HarnessFallbackError) or runtime_fallback_reason(exc):
        return route_retry.after(record)
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


def _credential_or_route_retry(
    preflight: "_Preflight",
    route_retry: _RouteRetry,
    route_fallback: _RouteFallback,
    lane: str,
) -> DispatchCredential | TaskAttempt:
    task = route_retry.task
    harness = preflight.dispatch.harness
    try:
        return _admission_park_or_child_env(
            task,
            harness,
            preflight.dispatch.provider,
            context=_ChildEnvDispatch(
                lane=lane,
                phase=route_retry.phase,
                route_candidate=preflight.dispatch.route_candidate_index is not None,
            ),
        )
    except HarnessFallbackError as exc:
        _restore_unconsumed_resume_thread(harness)
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
        return _record_failure(task, error=str(exc))  # no-usage: credential admission failed before any turn was billed


def _ready_run_or_failure(
    preflight: "_Preflight",
    route_retry: _RouteRetry,
    route_fallback: _RouteFallback,
    lane: str,
) -> tuple[DispatchCredential, _PreparedRun] | TaskAttempt:
    credential = _credential_or_route_retry(preflight, route_retry, route_fallback, lane)
    if isinstance(credential, TaskAttempt):
        return credential
    try:
        prepared = _prepare_run(
            route_retry.task,
            preflight,
            phase=route_retry.phase,
            handoff=route_retry.handoff,
            credential=credential,
        )
    except SkillDispatchError as exc:
        _restore_unconsumed_resume_thread(preflight.dispatch.harness)
        logger.warning("Refusing task %s: %s", route_retry.task.pk, exc)
        attempt = _record_failure(  # no-usage: required skill refused before the harness opened; no turn billed
            route_retry.task, error=str(exc), result={"skill_assurance": exc.assurance}
        )
        record_skill_assurance_attempt(route_retry.task, attempt)
        return attempt
    return credential, prepared


def run_agent(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> TaskAttempt:
    """Drive an agent for *task* in-process, holding its checkout for the whole run (#3952).

    The #3903 Task-seam dedupe cannot see an agent that ALREADY holds a checkout,
    so the loop's own agent and an operator-dispatched one land in one working
    tree and interleave commits. The claim is taken here — the outermost point of
    a dispatch, before the harness resolves — and released on every exit path.
    A refusal is a recorded FAILED attempt naming the incumbent: the run must not
    proceed into a tree someone else is editing, and it must not vanish silently.

    Nested INSIDE that claim, the drive runs inside
    :func:`~teatree.core.models.task_claim.drive_claim` (#4164), so a sweep can tell a
    memory-thrashed event loop that stalled past its 900s lease from a dead worker —
    in-process (the ``loops``-queue sweeps, sibling threads of this one) AND cross-process
    (``reclaim_orphaned_claims`` / ``reap_stale_claims``, which run inside the separate
    ``loops_tick`` subprocess every tick spawns). Without it a sweep reads the lapsed lease
    as death, fails the row (which does not kill this process) and enqueues a SECOND agent
    onto the same worktree.

    The order is load-bearing: occupancy refuses a tree another holder is already editing
    before any lease work happens, and ``drive_claim`` then keeps the lease alive for the
    run occupancy admitted. Collapsing either one re-opens the bug the other closes.
    """
    try:
        with (
            occupy_ticket_checkout(task.ticket, holder=task_holder_id(task), holder_session=task.claimed_by_session),
            drive_claim(task),
            delivered_phase_handoff(task) as handoff,
        ):
            return _run_agent(task, phase=phase, overlay_skill_metadata=overlay_skill_metadata, handoff=handoff)
    except WorktreeOccupiedError as exc:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: refused before the harness opened — no turn billed


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

    # Resolved HERE, not inside the coroutine (#3980): Django refuses a synchronous ORM read to a
    # thread that owns a running event loop, and the config resolver catches that refusal and
    # resolves the whole DB override tier as unreadable — so a ceiling read from inside
    # ``asyncio.run`` silently returns shipped defaults on every dispatch instead of failing.
    watchdog = LoopWatchdog.from_settings()
    try:
        # The quarantined reader (#116) also spawns inside ``reader_env_hermetic`` so its
        # ``os.environ`` is reduced to the allowlist: the SDK merges ``os.environ`` under
        # ``options.env`` and cannot delete an omitted key, so scrubbing here is the only
        # point the child is guaranteed credential-free (belt; ``options.env`` is the
        # suspenders). A no-op ``nullcontext`` for every non-reader phase.
        reader_scrub = reader_env_hermetic() if is_reader_phase(phase) else contextlib.nullcontext()
        with agent_spawn_env(), reader_scrub:
            outcome = asyncio.run(
                _drive_with_heartbeat(
                    task,
                    prepared.prompt,
                    prepared.options,
                    GuardedHarness(harness=harness, guard=prepared.compaction_guard),
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
        return _record_failure(task, error=str(exc))  # no-usage: the harness never opened, so no turn was billed
    except Exception:
        # AH-3 / #2916: a NON-CredentialError ``open()`` (or drive) failure must not
        # irrecoverably destroy a resumed task's parked thread either. ``resolve_dispatch_harness``
        # popped it when BUILDING the harness, and an escaping exception yields no outcome,
        # so the retain below never runs. Restore it so the resumed conversation survives for
        # a retry, then let the failure propagate: the caller (``tasks.py``) records the
        # durable failed attempt with the full traceback exactly as before.
        _restore_unconsumed_resume_thread(harness)
        raise

    # Retained before any outcome write, since the requeue sweep can see the row from that write on.
    retain_run_thread(task, outcome.thread)
    route_failure = _fallback_reason_for_outcome(
        outcome,
        metered_transport=harness.capabilities.metered_lane,
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
            ),
        )
    # #3673 Tier 3 provenance: resolve the per-tier effort the same way
    # ``_build_options`` pins it on the spawn (a deterministic settings read, so
    # the two resolutions never diverge) and pair it with the resolved skill
    # bundle, so the recorded attempt carries exactly what this dispatch ran with.
    attempt = _record_outcome(
        task,
        replace(outcome, compaction_stopped=prepared.compaction_guard.stopped_run),
        harness,
        CeilingSalvage(
            phase=phase,
            lane=lane,
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
    refusal = pre_harness_refusal(task, phase=phase)
    if refusal is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, refusal)
        return _record_failure(task, error=refusal)  # no-usage: refused before the harness opened — no turn billed

    stage_skills = _stage_skills_or_refusal(task, phase=phase)
    if isinstance(stage_skills, TaskAttempt):
        return stage_skills

    skills = resolve_skill_bundle(
        phase=phase,
        overlay_skill_metadata=overlay_skill_metadata,
        worktree_path=dispatch_worktree_path(task.ticket),
        stage_skills=stage_skills,
    )
    dispatch = _resolve_backend_or_failure(task, phase=phase, skills=skills)
    if isinstance(dispatch, TaskAttempt):
        return dispatch

    return _Preflight(stage_skills=stage_skills, dispatch=dispatch, skills=skills)


def _restore_unconsumed_resume_thread(harness: Harness) -> None:
    """Re-persist a resume thread popped but never actually driven (souliane/teatree#2916)."""
    restore = getattr(harness, "restore_unconsumed_resume_thread", None)
    if callable(restore):
        restore()


def _stage_skills_or_refusal(task: Task, *, phase: str) -> list[str] | TaskAttempt:
    """Every reason to refuse before the harness, then the dispatch's stage skills.

    Both refusals precede harness resolution (souliane/teatree#2916): for a resumed
    pydantic_ai task, resolving the harness destructively pops the parked ancestor's
    thread, so a run that will never start must not reach it or the conversation is
    lost. The skills are resolved ONCE here and threaded into every consumer (#3206);
    re-resolving per prompt builder re-warns on a misconfigured skill and re-reads its
    SKILL.md path for nothing.
    """
    budget_breach = TicketBudget.from_settings().breach_reason(task.ticket)
    if budget_breach is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, budget_breach)
        return _record_failure(task, error=budget_breach)  # no-usage: refused on budget — no turn billed
    try:
        return stage_skills_for_dispatch(phase)
    except ArchitecturalReviewSkillMissingError as exc:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: the skills never staged, so nothing was dispatched


def _resolve_backend_or_failure(
    task: Task, *, phase: str = "", skills: list[str] | None = None
) -> DispatchHarness | TaskAttempt:
    """Resolve the headless transport ONCE, or a recorded failure for an unresolvable backend."""
    try:
        return resolve_dispatch_harness(task, phase=phase or None, skills=skills)
    except (
        NotImplementedError,
        UnknownHarnessError,
        InvalidHarnessProviderError,
        CredentialError,
        AmbiguousSkillRouteError,
        ConflictingHarnessRoutingError,
    ) as exc:
        return _record_failure(task, error=str(exc))  # no-usage: an unimplemented/unresolvable backend never ran


def _admission_park_or_child_env(
    task: Task,
    harness: Harness,
    provider: AgentHarnessProvider | None,
    *,
    context: _ChildEnvDispatch = _DEFAULT_CHILD_ENV_DISPATCH,
) -> DispatchCredential | TaskAttempt:
    """Directive #3 admission guard, then the child-env resolution — one early-return seam."""
    admission_park = maybe_park_for_active_window(task, lane=context.lane)
    if admission_park is not None:
        _restore_unconsumed_resume_thread(harness)
        return admission_park
    return _resolve_child_env_or_failure(task, harness, provider, context=context)


def _resolve_child_env_or_failure(
    task: Task,
    harness: Harness,
    provider: AgentHarnessProvider | None,
    *,
    context: _ChildEnvDispatch = _DEFAULT_CHILD_ENV_DISPATCH,
) -> DispatchCredential | TaskAttempt:
    """Resolve the ``claude`` CLI child env for a :class:`~teatree.agents.harness.ClaudeSdkHarness` dispatch."""
    if not harness.capabilities.spawns_cli_child:
        return DispatchCredential()
    # The SDK spawns the ``claude`` CLI child; keep the same provisioning gate
    # the ``claude -p`` runner used.
    if shutil.which("claude") is None:
        if context.route_candidate:
            message = "claude is not installed"
            raise HarnessFallbackError(message, kind=HarnessFallbackKind.TRANSPORT)
        return _record_failure(
            task, error="claude is not installed"
        )  # no-usage: the CLI is absent, so nothing was dispatched
    try:
        resolved = _provider_child_env(provider, scope=_overlay_scope(task))
    except CredentialError as exc:
        # #C2: every configured account drained (an ``AllTokensExhaustedError``) → quiesce the
        # lane and auto-resume at the earliest reset rather than escalating to a human; any
        # other credential gap (or flag-off) records the loud terminal FAILED as before.
        if isinstance(exc, AllTokensExhaustedError):
            parked = park_task_on_all_exhausted(task, resets_at=exc.earliest_reset, lane=context.lane)
            if parked is not None:
                return parked
        if context.route_candidate:
            kind = HarnessFallbackKind.QUOTA if isinstance(exc, AllTokensExhaustedError) else HarnessFallbackKind.AUTH
            raise HarnessFallbackError(str(exc), kind=kind) from exc
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: the credential gap is pre-dispatch — no turn billed
    if is_reader_phase(context.phase):
        # A ``None`` env means "provider unset → use ambient os.environ"; the reader
        # instead pins exactly the allowlist (inference credential survives if ambiently
        # present, everything else dropped).
        ambient = resolved.env if resolved.env is not None else dict(os.environ)
        return replace(resolved, env=reader_child_env(ambient))
    capped = with_test_worker_cap(resolved.env, active_agents=_active_agent_count())
    return replace(resolved, env=with_routed_github_token(capped, overlay=_overlay_scope(task)))


def _active_agent_count() -> int:
    """Live headless agents in flight — the divisor for the test-worker budget (#3644/F9)."""
    return max(1, Task.objects.claimed_agent_count())


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
        ),
    )
