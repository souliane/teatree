"""Web terminal for interactive agent sessions via ttyd.

Spawns a ttyd process that wraps the interactive runtime CLI, making
the terminal session accessible from a browser at ``http://host:port``.
"""

import logging
import os
import shutil
import subprocess  # noqa: S404
from pathlib import Path

from teetree.agents.prompt import build_interactive_context
from teetree.agents.skill_bundle import resolve_skill_bundle
from teetree.core.models import Task, TaskAttempt
from teetree.core.overlay import SkillMetadata

logger = logging.getLogger(__name__)


def launch_web_session(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    host: str = "127.0.0.1",
) -> TaskAttempt:
    """Spawn a ttyd-wrapped interactive agent session.

    Returns a TaskAttempt with ``launch_url`` set to the web terminal URL.
    """
    port = _find_free_port()
    skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=overlay_skill_metadata)

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        msg = "claude CLI is not installed"
        raise FileNotFoundError(msg)

    ttyd_binary = shutil.which("ttyd")
    if ttyd_binary is None:
        msg = "ttyd is not installed. Install it with: brew install ttyd"
        raise FileNotFoundError(msg)

    resume_session_id = _get_resume_session_id(task)

    if resume_session_id:
        agent_command = [claude_bin, "--resume", resume_session_id]
        logger.info("Resuming headless session %s for task %s", resume_session_id, task.pk)
    else:
        system_context = build_interactive_context(task, skills=skills)
        agent_command = [claude_bin, "--append-system-prompt", system_context]

    proc = subprocess.Popen(  # noqa: S603
        [ttyd_binary, "--writable", "--port", str(port), "--once", *agent_command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_build_ttyd_env(),
    )
    logger.info("Launched ttyd session for task %s (pid=%s, port=%s)", task.pk, proc.pid, port)

    launch_url = f"http://{host}:{port}"
    return TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        launch_url=launch_url,
    )


_UUID_RE = __import__("re").compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _get_resume_session_id(task: Task) -> str:
    """Return the Claude session ID to resume, if available.

    The session_id is stored on the Session's agent_id when the interactive
    task was created as a followup from a headless run.
    """
    agent_id = task.session.agent_id if task.session else ""
    if agent_id and _UUID_RE.match(agent_id):
        return agent_id
    return ""


def _build_ttyd_env() -> dict[str, str]:
    env = os.environ.copy()
    if "TZ" not in env:
        tz = _detect_host_timezone()
        if tz:
            env["TZ"] = tz
    return env


def _detect_host_timezone() -> str:
    try:
        return Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def _find_free_port() -> int:
    import socket  # noqa: PLC0415

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 0))
        return s.getsockname()[1]
