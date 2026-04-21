"""Headless agent runner — executes tasks without a terminal.

Runs ``claude -p`` as a subprocess, captures structured output,
and stores the result in TaskAttempt.result for the dashboard to display.
"""

import json
import logging
import re
import shutil
import threading
from pathlib import Path

from django.utils import timezone

from teatree.agents.result_schema import RESULT_JSON_SCHEMA
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.worktree import Worktree
from teatree.skill_loading import SkillLoadingPolicy
from teatree.types import SkillMetadata
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 60  # seconds


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


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


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

    prompt = build_task_prompt(task)
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(phase)
    system_context = build_system_context(task, skills=skills, lifecycle_skill=lifecycle_skill)
    resume_session_id = _get_resume_session_id(task)
    command = _build_headless_command(binary, prompt, system_context, resume_session_id=resume_session_id)

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


def _run_with_heartbeat(task: Task, command: list[str], *, cwd: str | None = None) -> tuple[str, str, int]:
    """Run *command* as a subprocess while sending lease heartbeats.

    Returns ``(stdout, stderr, returncode)``.
    """
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(_HEARTBEAT_INTERVAL):
            try:
                task.renew_lease()
            except Exception:  # noqa: BLE001
                logger.warning("Heartbeat failed for task %s", task.pk)

    heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        proc = run_allowed_to_fail(command, cwd=cwd, expected_codes=None)
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=5)

    return proc.stdout, proc.stderr, proc.returncode


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


def _build_headless_command(binary: str, prompt: str, system_context: str, *, resume_session_id: str = "") -> list[str]:
    cmd = [binary]
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
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
        if last_attempt and last_attempt.agent_session_id and _UUID_RE.match(last_attempt.agent_session_id):
            return last_attempt.agent_session_id
        agent_id = current.session.agent_id if current.session_id else ""
        if agent_id and _UUID_RE.match(agent_id):
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
