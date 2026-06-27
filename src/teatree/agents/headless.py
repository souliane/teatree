"""Headless agent runner — executes tasks without a terminal.

Drives ``claude-agent-sdk`` in-process: builds a real-environment
:class:`~claude_agent_sdk.ClaudeAgentOptions`, runs the agent via
:class:`~claude_agent_sdk.ClaudeSDKClient`, captures the typed messages it
yields, and stores the result in ``TaskAttempt.result``. Unlike the clean-room
eval runner (``teatree.eval.api_runner``), this path runs a REAL task: it keeps
the developer's environment, skills, and context — no isolation, no
``setting_sources=[]``.

Wires only to ``Task`` / ``TaskAttempt`` models — no dashboard, no
process registry, no platform autostart.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
)
from claude_agent_sdk.types import RateLimitInfo, SystemPromptPreset
from django.conf import settings
from django.db import close_old_connections
from django.db.models import Sum
from django.utils import timezone

from teatree.agents.headless_usage import _attempt_usage
from teatree.agents.model_tiering import resolve_spawn_model
from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.config import AgentRuntime, get_effective_settings
from teatree.core.models import Task, TaskAttempt, Ticket
from teatree.core.models.worktree import Worktree
from teatree.llm.anthropic_limits import LimitMatch, classify_limit, classify_rate_limit_type
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential, CredentialError
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata

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

# Conservative documented default (#885 / #398-4): the per-ticket cumulative
# cost cap is opt-in. ``0.0`` = disabled, so installing this consumer changes
# no behaviour until the user configures a ceiling — the same precedent #882
# set for the watchdog's absolute cost dimension. The user picks a ceiling
# that matches their budget appetite once they want batch runs bounded.
_DEFAULT_TICKET_BUDGET = {
    "max_cost_usd": 0.0,  # 0 = disabled
}

# Headless agent default permission mode: a detached run has no human to grant
# tool permissions, so it bypasses the per-tool prompt and runs unattended.
_PERMISSION_MODE = "bypassPermissions"
# The SDK spawns no max-turns ceiling of its own; the loop watchdog bounds a
# runaway. ``0`` leaves the SDK uncapped (the watchdog is the real bound).
_MAX_TURNS = 0
# AskUserQuestion only renders to a live human at the harness — there is none
# in the SDK/headless lane, so leaving it allowed lets the agent silently stall
# on an unanswerable question. Hard-deny it: the agent must instead return the
# structured ``needs_user_input`` + ``user_input_reason`` and STOP, which the
# durable DeferredQuestion → Slack → resume loop then routes to the user.
_DISALLOWED_TOOLS = ("AskUserQuestion",)


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


@dataclass(frozen=True)
class TicketBudget:
    """Per-ticket cumulative cost cap consumer (#885 / #398-4).

    Where ``LoopWatchdog`` bounds a *single in-flight run* (it interrupts a
    runaway mid-run from the heartbeat thread), this consumer bounds the
    *whole ticket's lifetime spend* at dispatch time. Before a task's agent is
    launched it sums ``TaskAttempt.cost_usd`` across every task under the
    ticket; once the cumulative spend crosses the configured ceiling no
    further attempt is dispatched and a ``budget_exceeded`` ``TaskAttempt``
    failure is recorded (``task.fail()`` runs), surfacing the breach on the
    failure record. A ceiling of ``0.0`` disables the cap.
    """

    max_cost_usd: float

    @classmethod
    def from_settings(cls) -> "TicketBudget":
        configured = getattr(settings, "TEATREE_TICKET_BUDGET", None) or _DEFAULT_TICKET_BUDGET
        return cls(max_cost_usd=float(configured.get("max_cost_usd", 0.0)))

    def breach_reason(self, ticket: Ticket) -> str | None:
        """Return a reason string with the observed total, or ``None`` if healthy."""
        if not self.max_cost_usd:
            return None
        total = TaskAttempt.objects.filter(task__ticket=ticket).aggregate(cost=Sum("cost_usd"))["cost"] or 0.0
        if total > self.max_cost_usd:
            return (
                f"budget_exceeded: ticket spent ${total:.2f} > cap ${self.max_cost_usd:.2f} — refusing further dispatch"
            )
        return None


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

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
class _SdkOutcome:
    """The captured result of one in-process Agent-SDK run.

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


def run_headless(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> TaskAttempt:
    """Run a headless task in-process via ``claude-agent-sdk``."""
    from teatree.agents.prompt import build_system_context, build_task_prompt  # noqa: PLC0415

    runtime = get_effective_settings().agent_runtime
    if runtime is AgentRuntime.API:
        return _record_failure(
            task,
            error="agent_runtime=api (raw Anthropic Messages API runner) is not implemented yet; "
            "use sdk_oauth or sdk_apikey",
        )

    skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=overlay_skill_metadata)

    # The SDK spawns the ``claude`` CLI child; keep the same provisioning gate
    # the ``claude -p`` runner used.
    if shutil.which("claude") is None:
        return _record_failure(task, error="claude is not installed")

    try:
        child_env = _runtime_child_env(runtime)
    except CredentialError as exc:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, exc)
        return _record_failure(task, error=str(exc))

    budget_breach = TicketBudget.from_settings().breach_reason(task.ticket)
    if budget_breach is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, budget_breach)
        return _record_failure(task, error=budget_breach)

    prompt = build_task_prompt(task, skills=skills)
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(phase)
    system_context = build_system_context(task, skills=skills, lifecycle_skill=lifecycle_skill)
    options = _build_options(task, system_context, phase=phase, skills=skills, env=child_env)

    outcome = asyncio.run(_drive_with_heartbeat(task, prompt, options))

    failure = _outcome_failure(task, outcome)
    if failure is not None:
        return failure
    return _record_success(task, outcome, phase=phase)


def _outcome_failure(task: Task, outcome: _SdkOutcome) -> TaskAttempt | None:
    """Fold a non-success drive outcome into a recorded failure, or ``None``.

    Collapses the stuck-loop / usage-limit / error-result terminal cases into a
    single return so ``run_headless`` stays within its early-return budget.
    """
    if outcome.stuck_reason is not None:
        return _record_failure(task, error=f"{_STUCK_LOOP_PREFIX}{outcome.stuck_reason}")
    limit = _limit_match(outcome.result_message, outcome.rate_limit_info)
    if limit is not None:
        reason = limit.as_reason()
        logger.warning("Task %s hit a model-access limit (%s): %s", task.pk, limit.cause.value, reason)
        return _record_failure(task, error=reason)
    error_reason = _error_result_reason(outcome.result_message)
    if error_reason is not None:
        logger.warning("Task %s ended in a failed run: %s", task.pk, error_reason)
        return _record_failure(task, error=error_reason)
    return None


def _runtime_child_env(runtime: AgentRuntime) -> dict[str, str] | None:
    """The child-process env that pins the credential for a headless ``runtime``.

    ``sdk_apikey`` forces the metered ``ANTHROPIC_API_KEY`` (stripping the
    subscription token); ``sdk_oauth`` forces the subscription
    ``CLAUDE_CODE_OAUTH_TOKEN`` (stripping the API key) so the spawned ``claude``
    CLI rides the plan, not the meter. Any other runtime returns ``None`` — the
    ambient env is used unchanged (``interactive`` is dispatched in-session and
    ``api`` is refused upstream, so the runner only sees a headless runtime here).
    Raises :class:`CredentialError` when the selected token resolves from neither
    the env nor the ``pass`` store, so a misconfigured headless run fails loud.
    """
    if runtime is AgentRuntime.SDK_APIKEY:
        return AnthropicApiKeyCredential().child_env(os.environ)
    if runtime is AgentRuntime.SDK_OAUTH:
        return AnthropicSubscriptionCredential().child_env(os.environ)
    return None


def _build_options(
    task: Task,
    system_context: str,
    *,
    phase: str,
    skills: list[str],
    env: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    """Build the REAL-environment SDK options for a headless task.

    Mirrors what the deleted ``_build_headless_command`` passed: the appended
    system context, the resolved spawn model (the most-capable-wins floor merge
    of the per-phase tier and the per-skill MODEL floors of the loaded skills,
    else the user's default), the worktree as ``cwd`` / ``add_dirs``, and the
    parent session to resume. NO clean-room isolation — a headless run executes
    a real task and needs the real environment, skills, and project context.

    ``env`` (when supplied by :func:`_runtime_child_env`) pins the credential for
    the chosen ``agent_runtime`` on the spawned ``claude`` child; ``None`` leaves
    the SDK default (inherit the ambient env), byte-identical to before.
    """
    cwd = _resolve_task_cwd(task)
    add_dirs = [cwd] if cwd else []
    resume_session_id = _get_resume_session_id(task)
    # session_id + task pk are threaded so a situational honesty-critical
    # escalation (teatree#2263) can raise a verification spawn to the most-honest
    # model; both default absent → byte-identical to today when none is active.
    escalation_session_id = resume_session_id or (task.session.agent_id if task.session_id else "")  # ty: ignore[unresolved-attribute]
    options = ClaudeAgentOptions(
        # APPEND to the claude_code preset, never REPLACE it: a plain-str
        # system_prompt maps to --system-prompt (the deleted ``claude -p`` path
        # used --append-system-prompt), which would drop the Claude Code preset
        # on every production headless run.
        system_prompt=SystemPromptPreset(type="preset", preset="claude_code", append=system_context),
        model=resolve_spawn_model(
            phase,
            skills=skills,
            session_id=escalation_session_id or None,
            task_id=int(task.pk),
        )
        or None,
        cwd=cwd,
        add_dirs=add_dirs,
        permission_mode=_PERMISSION_MODE,
        disallowed_tools=list(_DISALLOWED_TOOLS),
        max_turns=_MAX_TURNS,
        resume=resume_session_id or None,
    )
    if env is not None:
        options.env = env
    return options


def _resolve_task_cwd(task: Task) -> str | None:
    """Determine the working directory for a task from its ticket's worktrees."""
    worktree = Worktree.objects.filter(ticket=task.ticket).order_by("pk").first()
    if worktree and Path(worktree.repo_path).is_dir():
        return str(worktree.repo_path)
    return None


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
    from django.db import connection  # noqa: PLC0415

    try:
        return TaskUsage.for_task(task)
    finally:
        connection.close()


async def _drive_with_heartbeat(
    task: Task,
    prompt: str,
    options: ClaudeAgentOptions,
    *,
    watchdog: LoopWatchdog | None = None,
) -> _SdkOutcome:
    """Run the agent in-process while sending lease heartbeats (#882, #997).

    A concurrent heartbeat coroutine renews the task lease each tick and, on a
    turn/cost ceiling breach, interrupts the SDK client so the in-flight agent
    can flush its final status before the run unwinds. The wall-clock ceiling
    is enforced with :func:`asyncio.wait_for`; a timeout interrupts the client
    and is reported as a runtime breach. DB reads/writes run in a worker thread
    so the event loop is never blocked.
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

    async with ClaudeSDKClient(options=options) as client:

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)
                    try:
                        await asyncio.to_thread(task.renew_lease)
                    except Exception:  # noqa: BLE001
                        logger.warning("Heartbeat failed for task %s", task.pk)
                    reason = watchdog.breach_reason(
                        task,
                        elapsed_seconds=time.monotonic() - started_at,
                        usage=usage,
                    )
                    if reason and not breach:
                        breach.append(reason)
                        logger.warning("Watchdog interrupting stuck task %s: %s", task.pk, reason)
                        await client.interrupt()
                        return
            finally:
                # This coroutine's thread-offloaded DB work owns its own
                # connection — close it so it is not leaked.
                await asyncio.to_thread(close_old_connections)

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            timeout = watchdog.max_runtime_seconds or None
            outcome = await asyncio.wait_for(_collect(client, prompt), timeout=timeout)
        except TimeoutError:
            await client.interrupt()
            elapsed = time.monotonic() - started_at
            reason = watchdog.breach_reason(task, elapsed_seconds=elapsed, usage=usage) or (
                f"runtime ceiling exceeded: ran {elapsed:.0f}s without exiting"
            )
            return _SdkOutcome(agent_text="", result_message=None, stuck_reason=reason)
        finally:
            heartbeat_task.cancel()

    if breach:
        return _SdkOutcome(
            agent_text=outcome.agent_text,
            result_message=outcome.result_message,
            stuck_reason=breach[0],
            rate_limit_info=outcome.rate_limit_info,
        )
    return outcome


async def _collect(client: ClaudeSDKClient, prompt: str) -> _SdkOutcome:
    """Send *prompt* and collect the agent's text + terminal ``ResultMessage`` + rejected window.

    A ``RateLimitEvent`` with ``status == "rejected"`` carries the SDK's typed
    ``rate_limit_type`` window — the unambiguous source the limit classifier
    prefers over prose-grep. The LAST rejected one is kept so a hard limit hit at
    the end of the stream classifies the failure precisely.
    """
    await client.query(prompt)
    text_parts: list[str] = []
    result_message: ResultMessage | None = None
    rate_limit_info: RateLimitInfo | None = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            text_parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
        elif isinstance(message, ResultMessage):
            result_message = message
        elif isinstance(message, RateLimitEvent) and message.rate_limit_info.status == "rejected":
            rate_limit_info = message.rate_limit_info
    return _SdkOutcome(
        agent_text="\n".join(text_parts),
        result_message=result_message,
        stuck_reason=None,
        rate_limit_info=rate_limit_info,
    )


def _record_success(task: Task, outcome: _SdkOutcome, *, phase: str = "") -> TaskAttempt:
    """Record a successful SDK run via the shared recorder.

    The schema-key check, the #1284 phase-evidence gate, and the
    complete/fail decision live once in ``attempt_recorder`` so the headless
    SDK path and the in-session ``record-attempt`` path can never drift on the
    result-envelope contract.
    """
    from teatree.agents.attempt_recorder import record_result_envelope  # noqa: PLC0415

    result = _parse_result(outcome.agent_text)
    if not result:
        result = {"summary": outcome.agent_text[:1000]}

    return record_result_envelope(task, result, phase=phase, usage=_attempt_usage(outcome.result_message))


def _get_resume_session_id(task: Task) -> str:
    """Walk the parent_task chain to find a resumable Claude session.

    When a headless task follows an interactive one (or vice versa),
    the session_id from the previous run lets us resume with full context.
    """
    current = task.parent_task
    while current is not None:
        last_attempt = current.attempts.order_by("-pk").first()
        if last_attempt and last_attempt.agent_session_id and UUID_RE.match(last_attempt.agent_session_id):
            return last_attempt.agent_session_id
        agent_id = current.session.agent_id if current.session_id else ""
        if agent_id and UUID_RE.match(agent_id):
            return agent_id
        current = current.parent_task
    return ""


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
