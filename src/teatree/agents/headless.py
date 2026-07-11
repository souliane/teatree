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
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, RateLimitEvent, ResultMessage, TextBlock
from claude_agent_sdk.types import RateLimitInfo
from django.conf import settings
from django.db import close_old_connections, connection
from django.db.models import Sum
from django.utils import timezone

from teatree.agents._headless_env import _overlay_scope, _provider_child_env
from teatree.agents._headless_options import _build_options
from teatree.agents.harness import (
    ClaudeSdkHarness,
    Harness,
    HarnessSession,
    PydanticAiHarness,
    pydantic_ai_thread,
    resolve_harness,
)
from teatree.agents.headless_budget import TicketBudget
from teatree.agents.headless_usage import _attempt_usage
from teatree.agents.pydantic_ai_resume import maybe_persist_on_park, persist_parked_thread
from teatree.agents.reader_profile import is_reader_phase, reader_child_env, reader_env_hermetic
from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.usage_window import maybe_park_for_active_window, park_task_on_limit
from teatree.config import AgentHarnessProvider, get_effective_settings
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.llm.anthropic_limits import LimitMatch, classify_limit, classify_rate_limit_type
from teatree.llm.credentials import CredentialError
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata
from teatree.utils.git_run import git_env_hermetic

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 60  # seconds

# Conservative documented default (#882): a generous wall-clock ceiling that
# only trips on a genuinely runaway agent that never returns — the canonical
# "Claude session spins on the same error" symptom. Absolute turn/cost budget
# caps are #398-4's responsibility, so they default off here.
_DEFAULT_WATCHDOG = {
    "max_runtime_seconds": 3 * 60 * 60,  # 3h — well past any healthy phase task
    "max_turns": 0,  # 0 = disabled
    "max_cost_usd": 0.0,  # 0 = disabled
}


@dataclass(frozen=True)
class TaskUsage:
    """Accumulated ``TaskAttempt`` deltas for one task.

    Sampled once on the main thread before the agent starts: ``num_turns`` /
    ``cost_usd`` only land in the DB *after* an attempt completes, so
    prior-attempt totals are static for the current run.
    """

    turns: int
    cost_usd: float

    @classmethod
    def for_task(cls, task: Task) -> "TaskUsage":
        attempts = task.attempts  # ty: ignore[unresolved-attribute]
        totals = attempts.aggregate(turns=Sum("num_turns"), cost=Sum("cost_usd"))
        return cls(turns=totals["turns"] or 0, cost_usd=totals["cost"] or 0.0)


@dataclass(frozen=True)
class LoopWatchdog:
    """Detects a stuck loop / cost spike during the heartbeat loop (#882).

    Evaluates the running task's wall-clock runtime plus the accumulated
    ``TaskAttempt.num_turns`` / ``cost_usd`` deltas. When a ceiling is
    crossed the heartbeat loop interrupts the agent and a ``stuck_loop``
    ``TaskAttempt`` failure is recorded with the observed deltas. A ceiling
    of ``0`` disables that dimension.
    """

    max_runtime_seconds: float
    max_turns: int
    max_cost_usd: float

    @classmethod
    def from_settings(cls) -> "LoopWatchdog":
        configured = getattr(settings, "TEATREE_LOOP_WATCHDOG", None) or _DEFAULT_WATCHDOG
        return cls(
            max_runtime_seconds=float(configured.get("max_runtime_seconds", 0)),
            max_turns=int(configured.get("max_turns", 0)),
            max_cost_usd=float(configured.get("max_cost_usd", 0.0)),
        )

    def breach_reason(self, task: Task, *, elapsed_seconds: float, usage: TaskUsage | None = None) -> str | None:
        """Return a reason string with observed deltas, or ``None`` if healthy.

        *usage* is the pre-sampled accumulated delta snapshot; when omitted
        it is read from *task* (convenience for callers outside the loop).
        """
        if self.max_runtime_seconds and elapsed_seconds > self.max_runtime_seconds:
            return (
                f"runtime ceiling exceeded: ran {elapsed_seconds:.0f}s "
                f"> {self.max_runtime_seconds:.0f}s without exiting"
            )
        if self.max_turns or self.max_cost_usd:
            if usage is None:
                usage = TaskUsage.for_task(task)
            if self.max_turns and usage.turns > self.max_turns:
                return f"turns ceiling exceeded: {usage.turns} turns > {self.max_turns} without progress"
            if self.max_cost_usd and usage.cost_usd > self.max_cost_usd:
                return f"cost ceiling exceeded: ${usage.cost_usd:.2f} > ${self.max_cost_usd:.2f} without progress"
        return None


_STUCK_LOOP_PREFIX = "stuck_loop: "
_RESULT_ERROR_PREFIX = "result_error: "


def _error_result_reason(message: ResultMessage | None) -> str | None:
    """Return a failure reason when the run did NOT complete cleanly, else ``None``.

    A missing terminal ``ResultMessage`` (the stream ended before the CLI emitted
    one) and a ``ResultMessage(is_error=True)`` that is NOT a usage-limit message
    are both genuine FAILED runs (#1764 class): they must record a failed attempt
    carrying the CLI's own ``result`` / ``errors`` / ``api_error_status``, never
    be laundered into a completion that advances the ticket FSM over a failed run.
    Called only AFTER :func:`_limit_match` has already claimed a limit error,
    so a limit message never reaches here.
    """
    if message is None:
        return f"{_RESULT_ERROR_PREFIX}no terminal ResultMessage — the run ended without completing"
    if not message.is_error:
        return None
    detail = str(message.result or "").strip()
    if not detail and message.errors:
        detail = "; ".join(str(err) for err in message.errors)
    status = message.api_error_status
    parts = [f"subtype={message.subtype}"]
    if status:
        parts.append(f"api_error_status={status}")
    if detail:
        parts.append(detail)
    return _RESULT_ERROR_PREFIX + " — ".join(parts)


def _limit_match(message: ResultMessage | None, rate_limit_info: RateLimitInfo | None = None) -> LimitMatch | None:
    """Return the classified :class:`LimitMatch`, or ``None`` when not a limit error.

    Keyed on ``is_error`` so a healthy result whose text merely discusses limits
    is never flagged. When the run IS an error and the stream carried a rejected
    :class:`~claude_agent_sdk.types.RateLimitInfo`, classify from its TYPED
    ``rate_limit_type`` window (unambiguous structured data — a ``seven_day_opus``
    is the WEEKLY cause, never a 5-hour one); otherwise fall back to phrase-matching
    the agent's final ``result`` string. Either way
    :func:`~teatree.llm.anthropic_limits.classify_limit` sorts it into its distinct
    cause (API-credit / subscription-session / subscription-weekly / rate-limit),
    so a credit-empty key is never reported as a subscription quota.
    """
    if message is None or not message.is_error:
        return None
    if rate_limit_info is not None and rate_limit_info.status == "rejected":
        typed = classify_rate_limit_type(rate_limit_info.rate_limit_type)
        if typed is not None:
            return typed
    return classify_limit(str(message.result or ""))


@dataclass(frozen=True)
class HarnessOutcome:
    """The captured result of one in-process harness-driven agent run.

    Exactly one of *stuck_reason* / *result* is meaningful: a watchdog breach
    sets *stuck_reason* and the run is recorded FAILED; otherwise the
    :class:`~claude_agent_sdk.ResultMessage` and the agent's final text drive a
    completed (or evidence-gated FAILED) attempt.
    """

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


def run_headless(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> TaskAttempt:
    """Run a headless task in-process via ``claude-agent-sdk``."""
    from teatree.agents.prompt import build_system_context, build_task_prompt  # noqa: PLC0415

    # Checked BEFORE resolving the harness (souliane/teatree#2916): for a
    # resumed pydantic_ai task, resolving the harness destructively pops the
    # parked ancestor's thread. A budget-breached ticket must never trigger
    # that pop, or the conversation is lost even though the run never starts.
    budget_breach = TicketBudget.from_settings().breach_reason(task.ticket)
    if budget_breach is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, budget_breach)
        return _record_failure(task, error=budget_breach)

    backend = _resolve_backend_or_failure(task, phase=phase)
    if isinstance(backend, TaskAttempt):
        return backend
    harness = backend

    skills = resolve_skill_bundle(
        phase=phase,
        overlay_skill_metadata=overlay_skill_metadata,
        worktree_path=dispatch_worktree_path(task.ticket),
    )

    provider = get_effective_settings().agent_harness_provider
    lane = _resolve_dispatch_lane(harness, provider)

    child_env_result = _admission_park_or_child_env(task, harness, provider, lane=lane, phase=phase)
    if isinstance(child_env_result, TaskAttempt):
        return child_env_result
    child_env = child_env_result

    prompt = build_task_prompt(task, skills=skills)
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(phase)
    system_context = build_system_context(task, skills=skills, lifecycle_skill=lifecycle_skill)
    options = _build_options(task, system_context, phase=phase, skills=skills, env=child_env)

    try:
        # The quarantined reader (#116) also spawns inside ``reader_env_hermetic`` so its
        # ``os.environ`` is reduced to the allowlist: the SDK merges ``os.environ`` under
        # ``options.env`` and cannot delete an omitted key, so scrubbing here is the only
        # point the child is guaranteed credential-free (belt; ``options.env`` is the
        # suspenders). A no-op ``nullcontext`` for every non-reader phase.
        reader_scrub = reader_env_hermetic() if is_reader_phase(phase) else contextlib.nullcontext()
        with git_env_hermetic(), reader_scrub:
            outcome = asyncio.run(_drive_with_heartbeat(task, prompt, options, harness))
    except CredentialError as exc:
        # A non-ClaudeSdkHarness resolves its own credential lazily inside
        # ``harness.open`` — this is the same "fail loud, record it" contract
        # the eager ``child_env`` catch above gives the ClaudeSdkHarness.
        # ``resolve_harness`` (above) already popped any resumed pydantic_ai
        # thread as a side effect of BUILDING the harness — restore it, since
        # a run that never opened never actually consumed it (#2916).
        _restore_unconsumed_resume_thread(harness)
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))

    failure = _outcome_failure(task, outcome, lane=lane)
    if failure is not None:
        return failure
    return _record_success(task, outcome, phase=phase, lane=lane)


def _restore_unconsumed_resume_thread(harness: Harness) -> None:
    """Re-persist a pydantic_ai resume thread popped but never actually driven.

    ``resolve_harness`` pops a resumed pydantic_ai task's parked thread as a
    side effect of BUILDING the harness — before ``harness.open()`` ever
    runs, the only point OrcaRouter's credential resolves. When ``open()``
    then fails, the popped thread would otherwise be silently and
    irrecoverably lost even though the run never happened
    (souliane/teatree#2916). A no-op for every other harness, and for a fresh
    (non-resumed) pydantic_ai dispatch.
    """
    if isinstance(harness, PydanticAiHarness) and harness.resume_source is not None and harness.history:
        persist_parked_thread(harness.resume_source, harness.history)


def _resolve_backend_or_failure(task: Task, *, phase: str = "") -> Harness | TaskAttempt:
    """Resolve the headless transport, or a recorded failure for an unimplemented backend.

    ``agent_harness`` selection itself (:func:`~teatree.agents.harness.resolve_harness`)
    never fails here — both the ``claude_sdk`` and ``pydantic_ai`` backends
    ([#2885](https://github.com/souliane/teatree/issues/2885)) are shipped;
    ``NotImplementedError`` is still caught below as a forward-compatible guard
    for a FUTURE reserved backend value.

    *phase* opts a ``pydantic_ai`` dispatch into the Lane-B tool layer (PR-03,
    souliane/teatree#2512): the resolved harness wires the phase-scoped, gated
    toolsets. Ignored for the ``claude_sdk`` backend.
    """
    try:
        return resolve_harness(task, phase=phase or None)
    except NotImplementedError as exc:
        return _record_failure(task, error=str(exc))


def _admission_park_or_child_env(
    task: Task, harness: Harness, provider: AgentHarnessProvider | None, *, lane: str, phase: str = ""
) -> dict[str, str] | TaskAttempt | None:
    """Directive #3 admission guard, then the child-env resolution — one early-return seam.

    While an uncleared usage window covers this dispatch's *lane*, park the task rather than
    burn an attempt that will 429 (a no-op when the flag is off or no window covers the
    lane). A park restores any resume thread ``resolve_harness`` popped, since the run never
    opened (mirrors the ``CredentialError`` path). Otherwise defers to
    :func:`_resolve_child_env_or_failure`. Returns a ``TaskAttempt`` (parked or failed) for
    an early return, or the child env (``dict``/``None``) to proceed.
    """
    admission_park = maybe_park_for_active_window(task, lane=lane)
    if admission_park is not None:
        _restore_unconsumed_resume_thread(harness)
        return admission_park
    return _resolve_child_env_or_failure(task, harness, provider, phase=phase)


def _resolve_child_env_or_failure(
    task: Task, harness: Harness, provider: AgentHarnessProvider | None, *, phase: str = ""
) -> dict[str, str] | TaskAttempt | None:
    """Resolve the ``claude`` CLI child env for a :class:`~teatree.agents.harness.ClaudeSdkHarness` dispatch.

    Only the ``ClaudeSdkHarness`` spawns the bundled ``claude`` CLI child and
    needs its Anthropic credential env — the ``claude`` binary provisioning
    check and the Layer-2 ``agent_harness_provider``-keyed credential resolution
    are both scoped to it. Any OTHER harness (e.g. ``PydanticAiHarness``,
    [#2885](https://github.com/souliane/teatree/issues/2885)) resolves its OWN
    credential lazily inside ``harness.open`` — this returns ``None``
    unconditionally for it (no CLI child, no child env), and that harness's
    ``CredentialError`` is caught by the broad guard around the drive call in
    ``run_headless``.

    For the #116 quarantined reader phase, the resolved env is filtered through
    :func:`~teatree.agents.reader_profile.reader_child_env` so ``options.env`` carries
    ONLY the inference credential + minimal runtime — never the full ambient env the
    provider base is built from (which would re-introduce every secret over the
    ``os.environ`` scrub). This is the suspenders to :func:`reader_env_hermetic`'s belt.
    """
    if not isinstance(harness, ClaudeSdkHarness):
        return None
    # The SDK spawns the ``claude`` CLI child; keep the same provisioning gate
    # the ``claude -p`` runner used.
    if shutil.which("claude") is None:
        return _record_failure(task, error="claude is not installed")
    try:
        base_env = _provider_child_env(provider, scope=_overlay_scope(task))
    except CredentialError as exc:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))
    if is_reader_phase(phase):
        # ``base_env is None`` means "provider unset → use ambient os.environ"; the
        # reader instead pins exactly the allowlist (inference credential survives if
        # ambiently present, everything else dropped).
        return reader_child_env(base_env if base_env is not None else dict(os.environ))
    return base_env


def _outcome_failure(task: Task, outcome: HarnessOutcome, *, lane: str = "") -> TaskAttempt | None:
    """Fold a non-success drive outcome into a recorded failure (or park), or ``None``.

    Collapses the stuck-loop / usage-limit / error-result terminal cases into a
    single return so ``run_headless`` stays within its early-return budget. A usage-limit
    hit is PARKED not FAILED when Directive #3 auto-recovery is enabled (the flag-off
    default records the terminal FAILED exactly as before).
    """
    if outcome.stuck_reason is not None:
        return _record_failure(task, error=f"{_STUCK_LOOP_PREFIX}{outcome.stuck_reason}")
    limit = _limit_match(outcome.result_message, outcome.rate_limit_info)
    if limit is not None:
        sdk_resets_at = outcome.rate_limit_info.resets_at if outcome.rate_limit_info is not None else None
        parked = park_task_on_limit(task, limit, sdk_resets_at=sdk_resets_at, lane=lane)
        if parked is not None:
            return parked
        reason = limit.as_reason()
        logger.warning("Task %s hit a model-access limit (%s): %s", task.pk, limit.cause.value, reason)
        return _record_failure(task, error=reason)
    error_reason = _error_result_reason(outcome.result_message)
    if error_reason is not None:
        logger.warning("Task %s ended in a failed run: %s", task.pk, error_reason)
        return _record_failure(task, error=error_reason)
    return None


# souliane/teatree#657: the Layer-2 lane (subscription vs metered) each
# ``AgentHarnessProvider`` authenticates through — ORCA_ROUTER_BYOK is a
# metered BYOK key, same lane as API_KEY.
_LANE_BY_PROVIDER: dict[AgentHarnessProvider, str] = {
    AgentHarnessProvider.SUBSCRIPTION_OAUTH: TaskAttempt.Lane.SUBSCRIPTION,
    AgentHarnessProvider.API_KEY: TaskAttempt.Lane.METERED,
    AgentHarnessProvider.ORCA_ROUTER_BYOK: TaskAttempt.Lane.METERED,
}


def _resolve_dispatch_lane(harness: Harness, provider: AgentHarnessProvider | None) -> str:
    """The Layer-2 lane (souliane/teatree#657/#2887) this dispatch authenticated through.

    A :class:`~teatree.agents.harness.PydanticAiHarness` run always rides
    OrcaRouter's BYOK metered credential — the only Layer-2 provider valid
    under ``agent_harness=pydantic_ai`` — so it is unconditionally METERED. A
    :class:`ClaudeSdkHarness` run is attributable only when an explicit
    Layer-2 pin (*provider*) was configured: the ambient-credential default
    (#2887, *provider* is ``None``) authenticates however the ``claude`` CLI's
    own login state resolves, which is unobservable here, so it stays
    unattributed (``""``) rather than guessing.
    """
    if isinstance(harness, PydanticAiHarness):
        return TaskAttempt.Lane.METERED
    if provider is None:
        return ""
    # A future AgentHarnessProvider member added without a matching entry
    # here must not surface as a KeyError: that would be caught by the
    # broad ``except Exception`` in ``tasks.py``'s SDK executor and record
    # an otherwise-successful, already-billed run as a FAILED attempt.
    return _LANE_BY_PROVIDER.get(provider, "")


def _sample_usage_closing_connection(task: Task) -> TaskUsage:
    """Sample :meth:`TaskUsage.for_task` and close THIS thread's DB connection.

    Run as an :func:`asyncio.to_thread` worker: the aggregate query opens a
    Django connection bound to the worker thread, which never closes itself.
    ``close_old_connections`` would NOT reap a fresh, healthy connection (it
    only closes ones past ``CONN_MAX_AGE`` / marked unusable), so close the
    thread-local connection explicitly — otherwise it outlives the thread and
    surfaces as a ``ResourceWarning: unclosed database`` when the thread is
    GC'd (an order-dependent test flake, and a real connection leak in
    production).
    """
    try:
        return TaskUsage.for_task(task)
    finally:
        connection.close()


async def _drive_with_heartbeat(
    task: Task,
    prompt: str,
    options: ClaudeAgentOptions,
    harness: Harness,
    *,
    watchdog: LoopWatchdog | None = None,
) -> HarnessOutcome:
    """Run the agent in-process while sending lease heartbeats (#882, #997).

    The *harness* opens the in-flight session (``harness.open(options)``); the
    driver talks to it through the narrow :class:`~teatree.agents.harness.HarnessSession`
    surface, so the transport backend is swappable behind the seam. A concurrent
    heartbeat coroutine renews the task lease each tick and, on a turn/cost
    ceiling breach, interrupts the session so the in-flight agent can flush its
    final status before the run unwinds. The wall-clock ceiling is enforced with
    :func:`asyncio.wait_for`; a timeout interrupts the session and is reported as
    a runtime breach. DB reads/writes run in a worker thread so the event loop is
    never blocked.
    """
    if watchdog is None:
        watchdog = LoopWatchdog.from_settings()

    # Sample accumulated deltas once before the run: prior-attempt totals are
    # static for this run. The read runs in a worker thread (so the event loop
    # is never blocked) that gets its OWN Django DB connection; close it in the
    # same thread or the connection outlives the thread and surfaces as a
    # ``ResourceWarning: unclosed database`` when the thread is GC'd (an
    # order-dependent test flake, and a real connection leak in production).
    usage = await asyncio.to_thread(_sample_usage_closing_connection, task)
    started_at = time.monotonic()
    breach: list[str] = []

    async with harness.open(options) as session:

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)
                    try:
                        await asyncio.to_thread(task.renew_lease)
                    except Exception:  # noqa: BLE001 — a heartbeat failure is logged, never breaks the watchdog loop
                        logger.warning("Heartbeat failed for task %s", task.pk)
                    reason = watchdog.breach_reason(
                        task,
                        elapsed_seconds=time.monotonic() - started_at,
                        usage=usage,
                    )
                    if reason and not breach:
                        breach.append(reason)
                        logger.warning("Watchdog interrupting stuck task %s: %s", task.pk, reason)
                        await session.interrupt()
                        return
            finally:
                # This coroutine's thread-offloaded DB work owns its own
                # connection — close it so it is not leaked.
                await asyncio.to_thread(close_old_connections)

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
        )
    return outcome


async def _collect(session: HarnessSession, prompt: str) -> HarnessOutcome:
    """Send *prompt* and collect the agent's text + terminal ``ResultMessage`` + rejected window.

    A ``RateLimitEvent`` with ``status == "rejected"`` carries the SDK's typed
    ``rate_limit_type`` window — the unambiguous source the limit classifier
    prefers over prose-grep. The LAST rejected one is kept so a hard limit hit at
    the end of the stream classifies the failure precisely.
    """
    await session.query(prompt)
    text_parts: list[str] = []
    result_message: ResultMessage | None = None
    rate_limit_info: RateLimitInfo | None = None
    async for message in session.receive_response():
        if isinstance(message, AssistantMessage):
            text_parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
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
    )


def _record_success(task: Task, outcome: HarnessOutcome, *, phase: str = "", lane: str = "") -> TaskAttempt:
    """Record a successful SDK run via the shared recorder.

    The schema-key check, the #1284 phase-evidence gate, and the
    complete/fail decision live once in ``attempt_recorder`` so the headless
    SDK path and the in-session ``record-attempt`` path can never drift on the
    result-envelope contract. *lane* is the resolved Layer-2 lane
    (souliane/teatree#657) this dispatch authenticated through.
    """
    from teatree.agents.attempt_recorder import record_result_envelope  # noqa: PLC0415

    result = _parse_result(outcome.agent_text)
    if not result:
        result = {"summary": outcome.agent_text[:1000]}

    maybe_persist_on_park(task, result, outcome.thread)  # (#2886)
    return record_result_envelope(task, result, phase=phase, usage=_attempt_usage(outcome.result_message, lane=lane))


def _parse_result(agent_text: str) -> dict[str, object]:
    """Extract structured result from the agent's text output.

    Tries to parse the last JSON object in the text (agents may print
    progress text before the final JSON result).
    """
    for raw_line in reversed(agent_text.strip().splitlines()):
        stripped = raw_line.strip()
        if stripped.startswith("{"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                continue
    return {}


def _validate_result(result: dict[str, object]) -> str:
    """Check that *result* only contains keys declared in the schema.

    Delegates to the shared :func:`~teatree.agents.attempt_recorder.validate_result_keys`
    so the headless and ``record-attempt`` paths enforce the identical
    ``additionalProperties: false`` rule.
    """
    from teatree.agents.attempt_recorder import validate_result_keys  # noqa: PLC0415

    return validate_result_keys(result)


def _record_failure(task: Task, *, exit_code: int = 1, error: str = "") -> TaskAttempt:
    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=exit_code,
        error=error,
    )
    task.fail()
    return attempt


def get_result_json_schema() -> dict[str, object]:
    """Return the JSON schema for structured agent output.

    Agents produce output matching this schema as a final JSON object.
    """
    return RESULT_JSON_SCHEMA
