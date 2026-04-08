"""Web terminal for interactive agent sessions via ttyd.

Spawns a ttyd process that wraps the interactive runtime CLI, making
the terminal session accessible from a browser at ``http://host:port``.
"""

import logging
import shutil

from teatree.agents.headless import _UUID_RE
from teatree.agents.prompt import build_interactive_context
from teatree.agents.services import get_terminal_mode
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.terminal_launcher import launch as terminal_launch
from teatree.core.models import Task, TaskAttempt
from teatree.types import SkillMetadata

logger = logging.getLogger(__name__)


def launch_web_session(
    task: Task,
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    terminal_mode: str = "",
    terminal_app: str = "",
) -> TaskAttempt:
    """Launch an interactive agent session using the configured terminal mode.

    Returns a TaskAttempt with ``launch_url`` set for browser-based modes,
    or empty for native terminal modes.
    """
    skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=overlay_skill_metadata)

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        msg = "claude CLI is not installed"
        raise FileNotFoundError(msg)

    resume_session_id = _get_resume_session_id(task)

    if resume_session_id:
        agent_command = [claude_bin, "--resume", resume_session_id]
        logger.info("Resuming headless session %s for task %s", resume_session_id, task.pk)
    else:
        system_context = build_interactive_context(task, skills=skills)
        agent_command = [claude_bin, "--append-system-prompt", system_context]

    mode = terminal_mode or get_terminal_mode()
    result = terminal_launch(agent_command, mode=mode, app=terminal_app)

    return TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        launch_url=result.launch_url,
    )


def _get_resume_session_id(task: Task) -> str:
    """Return the Claude session ID to resume, if available.

    The session_id is stored on the Session's agent_id when the interactive
    task was created as a followup from a headless run.
    """
    agent_id = task.session.agent_id if task.session else ""
    if agent_id and _UUID_RE.match(agent_id):
        return agent_id
    return ""
