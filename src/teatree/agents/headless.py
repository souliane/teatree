"""Headless agent runner — executes tasks without a terminal.

Runs ``claude -p`` as a subprocess, captures structured output,
and stores the result in ``TaskAttempt.result``. The runner is the
swap point for an Anthropic SDK runtime: a future implementation
that talks to the API directly need only provide a callable matching
``run_headless(task, *, phase, overlay_skill_metadata) -> TaskAttempt``.

Wires only to ``Task`` / ``TaskAttempt`` models — no dashboard, no
process registry, no platform autostart.
"""

import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Sum
from django.utils import timezone

from teatree.agents.model_tiering import resolve_phase_model
from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.core.models import Task, TaskAttempt, Ticket
from teatree.core.models.worktree import Worktree
from teatree.skill_loading import SkillLoadingPolicy
from teatree.types import SkillMetadata
from teatree.utils.run import PIPE, spawn

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 60  # seconds

# Conservative documented default (#882): a generous wall-clock ceiling that
# only trips on a genuinely runaway subprocess that never returns — the
# canonical "Claude session spins on the same error" symptom. Absolute
# turn/cost budget caps are #398-4's responsibility, so they default off here.
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


@dataclass(frozen=True)
class TaskUsage:
    """Accumulated ``TaskAttempt`` deltas for one task.

    Sampled once on the main thread before the subprocess starts:
    ``num_turns`` / ``cost_usd`` only land in the DB *after* an attempt
    completes, so prior-attempt totals are static for the current run.
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
    crossed the heartbeat loop terminates the subprocess and a
    ``stuck_loop`` ``TaskAttempt`` failure is recorded with the observed
    deltas. A ceiling of ``0`` disables that dimension.
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

    Where ``LoopWatchdog`` bounds a *single in-flight subprocess* (it kills
    a runaway mid-run from the heartbeat thread), this consumer bounds the
    *whole ticket's lifetime spend* at dispatch time. Before a task's
    subprocess is launched it sums ``TaskAttempt.cost_usd`` across every
    task under the ticket; once the cumulative spend crosses the configured
    ceiling no further attempt is dispatched and a ``budget_exceeded``
    ``TaskAttempt`` failure is recorded (``task.fail()`` runs), surfacing
    the breach on the failure record. A ceiling of ``0.0`` disables the cap.
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


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def run_headless(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
) -> TaskAttempt:
    """Run a headless task using ``claude -p``."""
    from teatree.agents.prompt import build_system_context, build_task_prompt  # noqa: PLC0415

    skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=overlay_skill_metadata)

    binary = shutil.which("claude")
    if binary is None:
        return _record_failure(task, error="claude is not installed")

    budget_breach = TicketBudget.from_settings().breach_reason(task.ticket)
    if budget_breach is not None:
        logger.warning("Refusing dispatch for task %s: %s", task.pk, budget_breach)
        return _record_failure(task, error=budget_breach)

    prompt = build_task_prompt(task)
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(phase)
    system_context = build_system_context(task, skills=skills, lifecycle_skill=lifecycle_skill)
    resume_session_id = _get_resume_session_id(task)
    model = resolve_phase_model(phase)
    command = _build_headless_command(
        binary,
        prompt,
        system_context,
        resume_session_id=resume_session_id,
        model=model,
    )

    cwd = _resolve_task_cwd(task)
    stdout, stderr, returncode = _run_with_heartbeat(task, command, cwd=cwd)

    if returncode != 0:
        return _record_failure(task, exit_code=returncode, error=stderr[:2000])

    envelope = _parse_cli_envelope(stdout)
    return _record_success(task, envelope)


def _resolve_task_cwd(task: Task) -> str | None:
    """Determine the working directory for a task from its ticket's worktrees."""
    ticket = task.ticket
    if ticket is None:
        return None
    worktree = Worktree.objects.filter(ticket=ticket).order_by("pk").first()
    if worktree and Path(worktree.repo_path).is_dir():
        return str(worktree.repo_path)
    return None


_STUCK_LOOP_EXIT_CODE = -9
_STUCK_LOOP_PREFIX = "stuck_loop: "


def _run_with_heartbeat(
    task: Task,
    command: list[str],
    *,
    cwd: str | None = None,
    watchdog: LoopWatchdog | None = None,
) -> tuple[str, str, int]:
    """Run *command* as a subprocess while sending lease heartbeats.

    The heartbeat loop doubles as a stuck-loop watchdog (#882): on each
    tick it samples the task's runtime / accumulated turn+cost deltas and,
    on a ceiling breach, terminates the subprocess. A watchdog kill returns
    a non-zero exit code with ``stuck_loop: <reason>`` on stderr so the
    caller records a ``stuck_loop`` ``TaskAttempt`` failure.

    Returns ``(stdout, stderr, returncode)``.
    """
    if watchdog is None:
        watchdog = LoopWatchdog.from_settings()

    # Sample accumulated deltas once on the main thread: prior-attempt
    # totals are static for this run and a threaded DB read would not see
    # the caller's transaction.
    usage = TaskUsage.for_task(task)

    stop_event = threading.Event()
    started_at = time.monotonic()
    proc = spawn(command, cwd=cwd, stdout=PIPE, stderr=PIPE)
    watchdog_reason: list[str] = []

    def _heartbeat() -> None:
        try:
            while not stop_event.wait(_HEARTBEAT_INTERVAL):
                try:
                    task.renew_lease()
                except Exception:  # noqa: BLE001
                    logger.warning("Heartbeat failed for task %s", task.pk)
                reason = watchdog.breach_reason(
                    task,
                    elapsed_seconds=time.monotonic() - started_at,
                    usage=usage,
                )
                if reason and not watchdog_reason:
                    watchdog_reason.append(reason)
                    logger.warning("Watchdog terminating stuck task %s: %s", task.pk, reason)
                    proc.kill()
        finally:
            # This thread owns its own DB connection — close it so the
            # connection is not leaked when the thread exits.
            close_old_connections()

    heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        # communicate() blocks until the process exits and reaps it; a
        # watchdog kill from the heartbeat thread unblocks it here.
        stdout, stderr = proc.communicate()
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=5)

    if watchdog_reason:
        return stdout or "", f"{_STUCK_LOOP_PREFIX}{watchdog_reason[0]}", _STUCK_LOOP_EXIT_CODE
    return stdout or "", stderr or "", proc.returncode


def _record_success(task: Task, envelope: dict[str, str]) -> TaskAttempt:
    agent_text = envelope.get("agent_text", "")
    result = _parse_result(agent_text)
    if not result:
        result = {"summary": agent_text[:1000]}

    schema_error = _validate_result(result)
    if schema_error:
        return _record_failure(task, exit_code=0, error=schema_error)

    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=0,
        result=result,
        agent_session_id=envelope.get("session_id", ""),
        input_tokens=_safe_int(envelope.get("input_tokens")),
        output_tokens=_safe_int(envelope.get("output_tokens")),
        cost_usd=_safe_float(envelope.get("cost_usd")),
        num_turns=_safe_int(envelope.get("num_turns")),
    )
    task.complete(result_artifact_path="")
    return attempt


def _build_headless_command(
    binary: str,
    prompt: str,
    system_context: str,
    *,
    resume_session_id: str = "",
    model: str | None = None,
) -> list[str]:
    cmd = [binary]
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["-p", prompt, "--append-system-prompt", system_context, "--output-format", "json"])
    return cmd


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


def _parse_cli_envelope(stdout: str) -> dict[str, str]:
    """Parse the Claude CLI JSON envelope to extract session_id, text, and usage.

    When ``--output-format json`` is used, stdout is a single JSON object
    with ``session_id`` and ``result`` (the agent's text output) at the top level.
    Usage stats (``cost_usd``, ``num_turns``, ``input_tokens``, ``output_tokens``)
    are extracted when present.  Falls back gracefully if stdout is not a CLI envelope.
    """
    try:
        envelope = json.loads(stdout)
        if isinstance(envelope, dict) and "session_id" in envelope:
            parsed: dict[str, str] = {
                "session_id": str(envelope.get("session_id", "")),
                "agent_text": str(envelope.get("result", "")),
            }
            for key in ("cost_usd", "num_turns", "input_tokens", "output_tokens"):
                if key in envelope:
                    parsed[key] = str(envelope[key])
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return {"agent_text": stdout, "session_id": ""}


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

    Returns an error message if validation fails, or an empty string on success.
    Full JSON Schema validation is intentionally avoided to keep the dependency
    footprint minimal — we only enforce the ``additionalProperties: false`` rule.
    """
    allowed = set(RESULT_JSON_SCHEMA.get("properties", {}).keys())  # type: ignore[union-attr]
    unexpected = set(result) - allowed
    if unexpected:
        return f"Agent result contains unexpected keys: {', '.join(sorted(unexpected))}"
    return ""


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

    Agents should produce output matching this schema when invoked with
    ``--output-format json``.
    """
    return RESULT_JSON_SCHEMA
