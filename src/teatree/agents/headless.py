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

from teatree.agents.model_tiering import resolve_spawn_model
from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.core.models import Task, TaskAttempt, Ticket
from teatree.core.models.worktree import Worktree
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata
from teatree.utils.run import PIPE, Popen, spawn

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

    prompt = build_task_prompt(task, skills=skills)
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(phase)
    system_context = build_system_context(task, skills=skills, lifecycle_skill=lifecycle_skill)
    resume_session_id = _get_resume_session_id(task)
    # Most-capable-wins floor merge of the phase model and the per-skill MODEL
    # floors of the loaded skills. MODEL only — claude -p never carries
    # --effort (effort is a session-wide pin on the interactive loop spawn).
    # session_id + task pk are threaded so a situational honesty-critical
    # escalation (teatree#2263) can raise a verification spawn to the most-honest
    # model; both default absent → byte-identical to today when none is active.
    escalation_session_id = resume_session_id or (task.session.agent_id if task.session_id else "")  # ty: ignore[unresolved-attribute]
    model = resolve_spawn_model(
        phase,
        skills=skills,
        session_id=escalation_session_id or None,
        task_id=int(task.pk),
    )
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
    return _record_success(task, envelope, phase=phase)


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

# Grace window between SIGTERM and SIGKILL when the watchdog terminates a
# breached subprocess (#997). SIGTERM first lets an in-flight agent flush its
# final status before the hard kill; SIGKILL escalates only if the process is
# still alive after the window. The 30s default matches the issue's proposal.
_WATCHDOG_TERM_GRACE_SECONDS = 30.0
_WATCHDOG_TERM_POLL_INTERVAL = 0.1


def _terminate_with_grace(proc: Popen[str], *, task_pk: object) -> None:
    """Drain-before-kill: SIGTERM, grace window, then SIGKILL if still alive (#997).

    A breached subprocess may have finished its work but not yet flushed its
    final status. SIGTERM gives it a chance to checkpoint and exit on its own;
    SIGKILL escalates only if the process ignores SIGTERM through the whole
    grace window. The poll loop returns the moment the process exits, so a
    cooperative agent is never hard-killed.
    """
    proc.terminate()
    deadline = time.monotonic() + _WATCHDOG_TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(_WATCHDOG_TERM_POLL_INTERVAL)
    if proc.poll() is None:
        logger.warning("Watchdog grace window elapsed for task %s — escalating to SIGKILL", task_pk)
        proc.kill()


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
    on a ceiling breach, terminates the subprocess via SIGTERM → grace
    window → SIGKILL (#997), so an in-flight agent can flush its final
    status before a hard kill. A watchdog kill returns a non-zero exit code
    with ``stuck_loop: <reason>`` on stderr so the caller records a
    ``stuck_loop`` ``TaskAttempt`` failure.

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
                    _terminate_with_grace(proc, task_pk=task.pk)
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


def _record_success(task: Task, envelope: dict[str, str], *, phase: str = "") -> TaskAttempt:
    """Record a ``claude -p`` envelope via the shared recorder.

    The schema-key check, the #1284 phase-evidence gate, and the
    complete/fail decision live once in ``attempt_recorder`` so the headless
    subprocess path and the in-session ``record-attempt`` path can never
    drift on the result-envelope contract.
    """
    from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope  # noqa: PLC0415

    agent_text = envelope.get("agent_text", "")
    result = _parse_result(agent_text)
    if not result:
        result = {"summary": agent_text[:1000]}

    model = envelope.get("model", "")
    usage = AttemptUsage(
        agent_session_id=envelope.get("session_id", ""),
        model=model,
        input_tokens=_safe_int(envelope.get("input_tokens")),
        output_tokens=_safe_int(envelope.get("output_tokens")),
        cache_read_tokens=_safe_int(envelope.get("cache_read_tokens")),
        cache_write_tokens=_safe_int(envelope.get("cache_write_tokens")),
        cost_usd=_resolve_cost_usd(envelope, model=model),
        num_turns=_safe_int(envelope.get("num_turns")),
    )
    return record_result_envelope(task, result, phase=phase, usage=usage)


def _resolve_cost_usd(envelope: dict[str, str], *, model: str) -> float | None:
    """Persist the CLI-reported cost when present, else the price-table estimate.

    Persisting an estimate at capture time means a row's ``cost_usd`` is never
    NULL once any token count was captured — the ``t3 cost`` report and the
    watchdog both read a real number rather than re-deriving it each query.
    Returns ``None`` only when nothing at all was captured.
    """
    reported = _safe_float(envelope.get("cost_usd"))
    if reported is not None:
        return reported
    token_keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    if all(envelope.get(key) is None for key in token_keys):
        return None
    from teatree.core.cost import AttemptUsage, price_table_cost_usd  # noqa: PLC0415

    return price_table_cost_usd(
        AttemptUsage(
            model=model or None,
            reported_cost_usd=None,
            input_tokens=_safe_int(envelope.get("input_tokens")) or 0,
            output_tokens=_safe_int(envelope.get("output_tokens")) or 0,
            cache_read_tokens=_safe_int(envelope.get("cache_read_tokens")) or 0,
            cache_write_tokens=_safe_int(envelope.get("cache_write_tokens")) or 0,
        ),
    )


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

    With ``--output-format json`` stdout is a single JSON object. ``session_id``
    and ``result`` (the agent's text output) and ``num_turns`` sit at the top
    level; the per-run cost is ``total_cost_usd`` (top level) and the token
    counts live in the nested ``usage`` object as ``input_tokens`` /
    ``output_tokens`` / ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens``. The model the run billed against is read from
    the single key of ``modelUsage`` (e.g. ``claude-opus-4-8[1m]``).

    Pre-2.x envelopes that put cost/tokens at the top level (``cost_usd`` and
    flat ``input_tokens``) are still honoured as a fallback so older transcripts
    parse. Falls back gracefully if stdout is not a CLI envelope.
    """
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {"agent_text": stdout, "session_id": ""}
    if not (isinstance(envelope, dict) and "session_id" in envelope):
        return {"agent_text": stdout, "session_id": ""}

    parsed: dict[str, str] = {
        "session_id": str(envelope.get("session_id", "")),
        "agent_text": str(envelope.get("result", "")),
    }
    if "num_turns" in envelope:
        parsed["num_turns"] = str(envelope["num_turns"])

    cost = envelope.get("total_cost_usd", envelope.get("cost_usd"))
    if cost is not None:
        parsed["cost_usd"] = str(cost)

    raw_usage = envelope.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    token_keys = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_tokens": "cache_read_input_tokens",
        "cache_write_tokens": "cache_creation_input_tokens",
    }
    for out_key, source in token_keys.items():
        if source in usage:
            parsed[out_key] = str(usage[source])
        elif source in envelope:  # pre-2.x flat fallback
            parsed[out_key] = str(envelope[source])

    # ``modelUsage`` is keyed by the billed model id (``claude-opus-4-8[1m]``);
    # a single-model run has one key. Absent on older envelopes — the attempt's
    # model stays unset and cost falls back to the reasoning tier.
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        parsed["model"] = str(next(iter(model_usage)))
    return parsed


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

    Agents should produce output matching this schema when invoked with
    ``--output-format json``.
    """
    return RESULT_JSON_SCHEMA
