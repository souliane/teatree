"""Headless agent runner — executes tasks without a terminal.

Drives an in-process agent session behind the
:class:`~teatree.agents.harness.Harness` seam: builds a real-environment
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
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import RateLimitInfo
from django.utils import timezone

from teatree.agents._headless_env import _overlay_scope, _provider_child_env, with_test_worker_cap
from teatree.agents._headless_options import SpawnOverrides, _build_options, resolve_headless_max_turns
from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR
from teatree.agents.harness import (
    Harness,
    HarnessSession,
    pydantic_ai_thread,
    resolve_dispatch_provider,
    resolve_harness,
)
from teatree.agents.harness_registry import InvalidHarnessProviderError, UnknownHarnessError
from teatree.agents.headless_budget import TicketBudget
from teatree.agents.headless_failure_taxonomy import error_result_reason as _error_result_reason
from teatree.agents.headless_failure_taxonomy import limit_match as _limit_match
from teatree.agents.headless_truncation import (
    alert_owner_max_tokens_truncation,
    alert_owner_max_turns_truncation,
    is_max_tokens_truncation,
    is_max_turns_truncation,
    max_turns_failure_reason,
)
from teatree.agents.headless_usage import DispatchProvenance, _attempt_usage
from teatree.agents.headless_watchdog import LoopWatchdog, TaskUsage, _sample_usage_closing_connection
from teatree.agents.model_tiering import resolve_spawn_effort
from teatree.agents.pydantic_ai_resume import maybe_persist_on_limit_park, maybe_persist_on_park
from teatree.agents.reader_profile import is_reader_phase, reader_child_env, reader_env_hermetic
from teatree.agents.result_schema import AgentResultBlob, ProseSummaryPolicy
from teatree.agents.skill_bundle import active_overlay_stage_skills, resolve_skill_bundle
from teatree.agents.usage_window import (
    LimitSignal,
    maybe_park_for_active_window,
    park_or_rotate_on_limit,
    park_task_on_all_exhausted,
)
from teatree.config import AgentHarnessProvider
from teatree.core.models import LeaseLostError, Task, TaskAttempt
from teatree.core.models.phase_landing import phase_landing_evidence
from teatree.core.models.task_claim import describe_lease_loss, drive_claim
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.credential_config import AllTokensExhaustedError
from teatree.llm.credentials import CredentialError
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata
from teatree.utils.git_run import git_env_hermetic
from teatree.utils.thread_db import close_thread_db_connections

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from teatree.agents.attempt_recorder import AttemptUsage

logger = logging.getLogger(__name__)

# ``LoopWatchdog`` / ``TaskUsage`` moved to ``headless_watchdog`` but stay part of
# this module's public surface (overlay_sdk re-exports ``LoopWatchdog``; tests patch
# ``headless.TaskUsage.for_task`` / ``headless._sample_usage_closing_connection``).
__all__ = ["HarnessOutcome", "LoopWatchdog", "TaskUsage", "run_headless"]

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
# false lapse, which absorbs realistic load spikes. A genuinely dead session's task
# still reclaims — just after the wider window.
_LEASE_SECONDS = 15 * _HEARTBEAT_INTERVAL  # 900s

_STUCK_LOOP_PREFIX = "stuck_loop: "

#: Truncation applied to the agent's raw text when it stands in for an envelope.
_PROSE_SUMMARY_CHARS = 1000


@dataclass(frozen=True)
class HarnessOutcome:
    """The captured result of one in-process harness-driven agent run."""

    agent_text: str
    result_message: ResultMessage | None
    stuck_reason: str | None
    #: The last REJECTED rate-limit window the stream carried (a ``RateLimitEvent``
    #: with ``status == "rejected"``), used to classify a limit failure from the
    #: SDK's unambiguous typed field. ``None`` when the stream named no rejected
    #: window — the classifier then falls back to phrase-matching the result text.
    rate_limit_info: RateLimitInfo | None = None
    #: (#2886) The pydantic_ai session's conversation, ``None`` for every other backend.
    thread: "list[ModelMessage] | None" = None
    #: (#3982) Whether ``stuck_reason`` is a LOST LEASE rather than a watchdog breach. A
    #: typed flag, not a phrase match on the reason: the reason now names the actual
    #: reclaimer, so any discriminator built on its wording would drift with it.
    lease_lost: bool = False
    #: ``ToolUseBlock``s the run emitted. Both backends yield tool use in this same
    #: vocabulary, so the count is lane-agnostic evidence that the agent ACTED —
    #: what :mod:`teatree.agents.action_verification` gates an acting phase on. A
    #: watchdog breach leaves it at the count observed before the breach.
    tool_calls: int = 0


def run_headless(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> TaskAttempt:
    """Drive an agent for *task* in-process via the ``agent_harness`` backend.

    The drive runs inside :func:`~teatree.core.models.task_claim.drive_claim` (#4164), so
    a sweep can tell a memory-thrashed event loop that stalled past its 900s lease from a
    dead worker — in-process (the ``loops``-queue sweeps, sibling threads of this one) AND
    cross-process (``reclaim_orphaned_claims`` / ``reap_stale_claims``, which run inside the
    separate ``loops_tick`` subprocess every tick spawns). Without it a sweep reads the
    lapsed lease as death, fails the row (which does not kill this process) and enqueues a
    SECOND agent onto the same worktree.
    """
    with drive_claim(task):
        return _run_headless_agent(task, phase=phase, overlay_skill_metadata=overlay_skill_metadata)


def _run_headless_agent(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> TaskAttempt:
    """Drive an agent for *task* in-process via the ``agent_harness`` backend."""
    from teatree.agents.prompt import build_system_context, build_task_prompt  # noqa: PLC0415 — lazy import

    # Checked BEFORE resolving the harness (souliane/teatree#2916): for a
    # resumed pydantic_ai task, resolving the harness destructively pops the
    # parked ancestor's thread. A budget-breached ticket must never trigger
    # that pop, or the conversation is lost even though the run never starts.
    budget_breach = TicketBudget.from_settings().breach_reason(task.ticket)
    if budget_breach is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, budget_breach)
        return _record_failure(
            task, error=budget_breach
        )  # no-usage: refused before the harness opened — no turn billed

    harness = _resolve_backend_or_failure(task, phase=phase)
    if isinstance(harness, TaskAttempt):
        return harness

    # Resolve the overlay's stage skills ONCE and thread the list into every
    # consumer (#3206). Re-resolving per prompt builder re-warns on a
    # misconfigured skill and re-reads its SKILL.md path for nothing.
    stage_skills = active_overlay_stage_skills(phase)
    skills = resolve_skill_bundle(
        phase=phase,
        overlay_skill_metadata=overlay_skill_metadata,
        worktree_path=dispatch_worktree_path(task.ticket),
        stage_skills=stage_skills,
    )

    # Resolved through the SAME task-overlay settings scope and the SAME phase pin the
    # harness above came from, so the transport and the credential can never disagree
    # about which harness this dispatch is running.
    provider = resolve_dispatch_provider(task, phase=phase)
    lane = _resolve_dispatch_lane(harness, provider)

    child_env = _admission_park_or_child_env(task, harness, provider, lane=lane, phase=phase)
    if isinstance(child_env, TaskAttempt):
        return child_env

    prompt = build_task_prompt(task, skills=skills, stage_skills=stage_skills)
    system_context = build_system_context(
        task,
        skills=skills,
        lifecycle_skill=SkillLoadingPolicy.lifecycle_for_phase(phase),
        stage_skills=stage_skills,
    )
    options = _build_options(
        task,
        system_context,
        phase=phase,
        skills=skills,
        overrides=SpawnOverrides(env=child_env, turn_ceiling=_turn_ceiling(harness)),
    )

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
        with git_env_hermetic(), reader_scrub:
            outcome = asyncio.run(_drive_with_heartbeat(task, prompt, options, harness, watchdog=watchdog))
    except CredentialError as exc:
        # A non-ClaudeSdkHarness resolves its own credential lazily inside
        # ``harness.open`` — this is the same "fail loud, record it" contract
        # the eager ``child_env`` catch above gives the ClaudeSdkHarness.
        # ``resolve_harness`` (above) already popped any resumed pydantic_ai
        # thread as a side effect of BUILDING the harness — restore it, since
        # a run that never opened never actually consumed it (#2916).
        _restore_unconsumed_resume_thread(harness)
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: the harness never opened, so no turn was billed
    except Exception:
        # AH-3 / #2916: a NON-CredentialError ``open()`` (or drive) failure must not
        # irrecoverably destroy a resumed task's parked thread either. ``resolve_harness``
        # popped it when BUILDING the harness, and the thread is re-persisted ONLY on the
        # success path (``_record_success`` below, outside this try) — never reached here.
        # Restore it so the resumed conversation survives for a retry, then let the failure
        # propagate: the caller (``tasks.py``) records the durable failed attempt with the
        # full traceback exactly as before, so only the thread-loss changes.
        _restore_unconsumed_resume_thread(harness)
        raise

    failure = _outcome_failure(task, outcome, phase=phase, lane=lane)
    if failure is not None:
        return failure
    # #3673 Tier 3 provenance: resolve the per-tier effort the same way
    # ``_build_options`` pins it on the spawn (a deterministic settings read, so
    # the two resolutions never diverge) and pair it with the resolved skill
    # bundle, so the recorded attempt carries exactly what this dispatch ran with.
    return _record_success(
        task,
        outcome,
        phase=phase,
        lane=lane,
        provenance=DispatchProvenance(reasoning_effort=resolve_spawn_effort(phase) or "", skills_loaded=tuple(skills)),
    )


def _turn_ceiling(harness: Harness) -> int:
    """The per-run turn cap for THIS dispatch's backend — the ``claude_sdk`` lane's, or none."""
    return resolve_headless_max_turns() if harness.capabilities.spawns_cli_child else 0


def _restore_unconsumed_resume_thread(harness: Harness) -> None:
    """Re-persist a resume thread popped but never actually driven (souliane/teatree#2916)."""
    restore = getattr(harness, "restore_unconsumed_resume_thread", None)
    if callable(restore):
        restore()


def _resolve_backend_or_failure(task: Task, *, phase: str = "") -> Harness | TaskAttempt:
    """Resolve the headless transport, or a recorded failure for an unimplemented backend."""
    try:
        return resolve_harness(task, phase=phase or None)
    except (NotImplementedError, UnknownHarnessError, InvalidHarnessProviderError, CredentialError) as exc:
        return _record_failure(task, error=str(exc))  # no-usage: an unimplemented/unresolvable backend never ran


def _admission_park_or_child_env(
    task: Task, harness: Harness, provider: AgentHarnessProvider | None, *, lane: str, phase: str = ""
) -> dict[str, str] | TaskAttempt | None:
    """Directive #3 admission guard, then the child-env resolution — one early-return seam."""
    admission_park = maybe_park_for_active_window(task, lane=lane)
    if admission_park is not None:
        _restore_unconsumed_resume_thread(harness)
        return admission_park
    return _resolve_child_env_or_failure(task, harness, provider, lane=lane, phase=phase)


def _resolve_child_env_or_failure(
    task: Task, harness: Harness, provider: AgentHarnessProvider | None, *, lane: str = "", phase: str = ""
) -> dict[str, str] | TaskAttempt | None:
    """Resolve the ``claude`` CLI child env for a :class:`~teatree.agents.harness.ClaudeSdkHarness` dispatch."""
    if not harness.capabilities.spawns_cli_child:
        return None
    # The SDK spawns the ``claude`` CLI child; keep the same provisioning gate
    # the ``claude -p`` runner used.
    if shutil.which("claude") is None:
        return _record_failure(
            task, error="claude is not installed"
        )  # no-usage: the CLI is absent, so nothing was dispatched
    try:
        base_env = _provider_child_env(provider, scope=_overlay_scope(task))
    except CredentialError as exc:
        # #C2: every configured account drained (an ``AllTokensExhaustedError``) → quiesce the
        # lane and auto-resume at the earliest reset rather than escalating to a human; any
        # other credential gap (or flag-off) records the loud terminal FAILED as before.
        if isinstance(exc, AllTokensExhaustedError):
            parked = park_task_on_all_exhausted(task, resets_at=exc.earliest_reset, lane=lane)
            if parked is not None:
                return parked
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))  # no-usage: the credential gap is pre-dispatch — no turn billed
    if is_reader_phase(phase):
        # ``base_env is None`` means "provider unset → use ambient os.environ"; the
        # reader instead pins exactly the allowlist (inference credential survives if
        # ambiently present, everything else dropped).
        return reader_child_env(base_env if base_env is not None else dict(os.environ))
    return with_test_worker_cap(base_env, active_agents=_active_agent_count())


def _active_agent_count() -> int:
    """Live headless agents in flight — the divisor for the test-worker budget (#3644/F9)."""
    return max(1, Task.objects.live_headless_agent_count())


def _outcome_failure(task: Task, outcome: HarnessOutcome, *, phase: str = "", lane: str = "") -> TaskAttempt | None:
    """Fold a non-success drive outcome into a recorded failure (or park), or ``None``.

    Every branch here is POST-turn — the agent ran and the SDK reported a result — so each
    carries the drive's own usage onto the recorded attempt (#4164). A park is the one
    exception and records its own row.
    """
    # Sampled once from the SAME ``ResultMessage`` every branch below classifies, so the
    # recorded spend can never describe a different run than the recorded error.
    usage = _attempt_usage(outcome.result_message, lane=lane, tool_calls=outcome.tool_calls)
    if outcome.stuck_reason is not None:
        return _record_stuck_outcome(task, outcome, stuck_reason=outcome.stuck_reason, usage=usage)
    limit = _limit_match(outcome.result_message, outcome.rate_limit_info)
    if limit is not None:
        sdk_resets_at = outcome.rate_limit_info.resets_at if outcome.rate_limit_info is not None else None
        signal = LimitSignal(sdk_resets_at=sdk_resets_at, usage=usage)
        parked = park_or_rotate_on_limit(task, limit, lane=lane, signal=signal)
        if parked is not None:
            maybe_persist_on_limit_park(task, outcome.thread)
            return parked
        reason = limit.as_reason()
        logger.warning("Task %s hit a model-access limit (%s): %s", task.pk, limit.cause.value, reason)
        return _record_failure(task, error=reason, usage=usage)
    if is_max_turns_truncation(outcome.result_message):
        reason = max_turns_failure_reason(outcome.result_message)
        alert_owner_max_turns_truncation(task, phase=phase, message=outcome.result_message)
        logger.warning("Task %s stopped at the turn ceiling: %s", task.pk, reason)
        return _record_failure(task, error=reason, usage=usage)
    error_reason = _error_result_reason(outcome.result_message)
    if error_reason is not None:
        if is_max_tokens_truncation(outcome.result_message):
            alert_owner_max_tokens_truncation(task, phase=phase)
        logger.warning("Task %s ended in a failed run: %s", task.pk, error_reason)
        return _record_failure(task, error=error_reason, usage=usage)
    return None


# souliane/teatree#657: the Layer-2 lane (subscription vs metered) each
# ``AgentHarnessProvider`` authenticates through — OPENAI_COMPATIBLE is a
# metered BYOK key, same lane as API_KEY.
_LANE_BY_PROVIDER: dict[AgentHarnessProvider, str] = {
    AgentHarnessProvider.SUBSCRIPTION_OAUTH: TaskAttempt.Lane.SUBSCRIPTION,
    AgentHarnessProvider.API_KEY: TaskAttempt.Lane.METERED,
    AgentHarnessProvider.OPENAI_COMPATIBLE: TaskAttempt.Lane.METERED,
}


def _resolve_dispatch_lane(harness: Harness, provider: AgentHarnessProvider | None) -> str:
    """The Layer-2 lane (souliane/teatree#657/#2887) this dispatch authenticated through."""
    if harness.capabilities.metered_lane:
        return TaskAttempt.Lane.METERED
    if provider is None:
        return ""
    # A future AgentHarnessProvider member added without a matching entry
    # here must not surface as a KeyError: that would be caught by the
    # broad ``except Exception`` in ``tasks.py``'s SDK executor and record
    # an otherwise-successful, already-billed run as a FAILED attempt.
    return _LANE_BY_PROVIDER.get(provider, "")


def _renew_lease_closing_connection(task: Task) -> None:
    """Renew *task*'s lease and close THIS thread's DB connection.

    A lost lease is re-raised naming what actually took the claim (#3982). The diagnosis
    is a read-back, so it must run in THIS thread — the ``finally`` below closes the only
    DB connection this thread owns.
    """
    try:
        task.renew_lease(lease_seconds=_LEASE_SECONDS)
    except LeaseLostError as exc:
        raise LeaseLostError(describe_lease_loss(task)) from exc
    finally:
        close_thread_db_connections()


async def _drive_with_heartbeat(
    task: Task,
    prompt: str,
    options: ClaudeAgentOptions,
    harness: Harness,
    *,
    watchdog: LoopWatchdog,
) -> HarnessOutcome:
    """Run the agent in-process while sending lease heartbeats (#882, #997).

    *watchdog* is REQUIRED rather than resolved here: its ceilings come from the DB-home config
    tier, and this coroutine runs inside the event loop where that read is refused (#3980).
    """
    # Sample accumulated deltas once before the run: prior-attempt totals are
    # static for this run. The read runs in a worker thread (so the event loop
    # is never blocked) that gets its OWN Django DB connection; close it in the
    # same thread or the connection outlives the thread and surfaces as a
    # ``ResourceWarning: unclosed database`` when the thread is GC'd (an
    # order-dependent test flake, and a real connection leak in production).
    usage = await asyncio.to_thread(_sample_usage_closing_connection, task)
    started_at = time.monotonic()
    breach: list[str] = []
    lease_lost = False

    async with harness.open(options) as session:

        async def _heartbeat() -> None:
            nonlocal lease_lost
            try:
                while True:
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)
                    try:
                        await asyncio.to_thread(_renew_lease_closing_connection, task)
                    except LeaseLostError as exc:
                        # Something took over this task's claim (the lease lapsed and was
                        # reclaimed). Abort THIS run — two drivers on the same unit is the
                        # double-spend the CAS guards. The reason names the actual
                        # reclaimer, which is often this very process (#3982).
                        breach.append(str(exc))
                        lease_lost = True
                        logger.warning("Task %s lease lost; interrupting duplicate run", task.pk)
                        await session.interrupt()
                        return
                    except Exception:
                        logger.warning("Heartbeat failed for task %s", task.pk, exc_info=True)
                    # Re-sample the accumulated turn/cost deltas each tick (F9.3) so the
                    # turn/cost ceilings observe the CURRENT run's spend, not the pre-run
                    # static snapshot — the "cost spike DURING the heartbeat loop" the
                    # watchdog docstring promises. Only pay the DB read when a turn/cost
                    # ceiling is armed (the runtime ceiling needs no usage).
                    live_usage = usage
                    if watchdog.max_turns or watchdog.max_cost_usd:
                        live_usage = await asyncio.to_thread(_sample_usage_closing_connection, task)
                    reason = watchdog.breach_reason(
                        task,
                        elapsed_seconds=time.monotonic() - started_at,
                        usage=live_usage,
                    )
                    if reason and not breach:
                        breach.append(reason)
                        logger.warning("Watchdog interrupting stuck task %s: %s", task.pk, reason)
                        await session.interrupt()
                        return
            finally:
                # Belt to the per-call suspenders: every offload target above
                # closes its own thread's handle, so this only reaps a worker
                # thread the pool happened to reuse without a closing target.
                await asyncio.to_thread(close_thread_db_connections)

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            timeout = watchdog.max_runtime_seconds or None
            outcome = await asyncio.wait_for(_collect(session, prompt), timeout=timeout)
        except TimeoutError:
            await session.interrupt()
            elapsed = time.monotonic() - started_at
            reason = watchdog.breach_reason(task, elapsed_seconds=elapsed, usage=usage) or (
                f"runtime ceiling exceeded: ran {elapsed:.0f}s without exiting"
            )
            return HarnessOutcome(agent_text="", result_message=None, stuck_reason=reason)
        finally:
            heartbeat_task.cancel()

    if breach:
        return HarnessOutcome(
            agent_text=outcome.agent_text,
            result_message=outcome.result_message,
            stuck_reason=breach[0],
            rate_limit_info=outcome.rate_limit_info,
            lease_lost=lease_lost,
        )
    return outcome


async def _collect(session: HarnessSession, prompt: str) -> HarnessOutcome:
    """Send *prompt* and collect the agent's text + terminal ``ResultMessage`` + rejected window."""
    await session.query(prompt)
    text_parts: list[str] = []
    result_message: ResultMessage | None = None
    rate_limit_info: RateLimitInfo | None = None
    tool_calls = 0
    async for message in session.receive_response():
        if isinstance(message, AssistantMessage):
            text_parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
            tool_calls += sum(1 for block in message.content if isinstance(block, ToolUseBlock))
        elif isinstance(message, ResultMessage):
            result_message = message
        elif isinstance(message, RateLimitEvent) and message.rate_limit_info.status == "rejected":
            rate_limit_info = message.rate_limit_info
    return HarnessOutcome(
        agent_text="\n".join(text_parts),
        result_message=result_message,
        stuck_reason=None,
        rate_limit_info=rate_limit_info,
        thread=pydantic_ai_thread(session),  # (#2886) captured while `session` is still open
        tool_calls=tool_calls,
    )


def _record_success(
    task: Task,
    outcome: HarnessOutcome,
    *,
    phase: str = "",
    lane: str = "",
    provenance: DispatchProvenance | None = None,
) -> TaskAttempt:
    """Record a successful SDK run via the shared recorder."""
    from teatree.agents.attempt_recorder import record_result_envelope  # noqa: PLC0415 — deferred: call-time import
    from teatree.agents.headless_result import parse_result  # noqa: PLC0415 — deferred: call-time import

    provenance = provenance or DispatchProvenance()
    usage = _attempt_usage(
        outcome.result_message,
        lane=lane,
        reasoning_effort=provenance.reasoning_effort,
        skills_loaded=list(provenance.skills_loaded),
        tool_calls=outcome.tool_calls,
    )
    parsed = parse_result(outcome.agent_text)
    result = parsed
    if not parsed:
        prose: AgentResultBlob = {"summary": outcome.agent_text[:_PROSE_SUMMARY_CHARS]}
        if not ProseSummaryPolicy.allowed(phase or task.phase):
            logger.warning("Task %s produced no result envelope; refusing to record success", task.pk)
            return _record_failure(task, exit_code=0, error=NO_ENVELOPE_ERROR, result=prose, usage=usage)
        result = prose

    maybe_persist_on_park(task, result, outcome.thread)  # (#2886)
    return record_result_envelope(task, result, phase=phase, usage=usage, envelope_parsed=bool(parsed))


def _record_stuck_outcome(
    task: Task, outcome: HarnessOutcome, *, stuck_reason: str, usage: "AttemptUsage | None" = None
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
        return _record_noop_over_completed_row(task, interruption=stuck_reason, usage=usage)
    evidence = phase_landing_evidence(task, trust_phase_artifact=True) if outcome.lease_lost else ""
    if evidence:
        return _record_landed(task, evidence=evidence, lease_loss=stuck_reason, usage=usage)
    return _record_failure(task, error=f"{_STUCK_LOOP_PREFIX}{stuck_reason}", usage=usage)


def _record_noop_over_completed_row(
    task: Task, *, interruption: str, usage: "AttemptUsage | None" = None
) -> TaskAttempt:
    """Record the interruption of a run whose row had already COMPLETED — exit-0, no failure.

    The row is left exactly as it is: this run is the one that has nothing to say about it.
    """
    logger.info("Task %s was interrupted after its row completed: %s", task.pk, interruption)
    return _record_interrupted_attempt(task, summary=f"the row had already completed — {interruption}", usage=usage)


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
    Task.objects.filter(pk=task.pk, status=Task.Status.PENDING).update(
        status=Task.Status.COMPLETED,
        claimed_at=None,
        claimed_by="",
        claimed_by_session="",
        lease_expires_at=None,
        heartbeat_at=None,
        owner_pid=None,
        owner_pid_namespace="",
    )
    logger.warning("Task %s lost its lease but its phase landed: %s", task.pk, evidence)
    return attempt


def _record_interrupted_attempt(task: Task, *, summary: str, usage: "AttemptUsage | None" = None) -> TaskAttempt:
    """The exit-0 attempt an interruption records when it is not the verdict on the work."""
    from teatree.agents.attempt_recorder import usage_fields  # noqa: PLC0415 — deferred: call-time import

    return TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=0,
        error="",
        result={"summary": summary},
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
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=exit_code,
        error=error,
        result=result or {},
        **usage_fields(usage),
    )
    task.fail(reason=error)
    return attempt
