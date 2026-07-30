"""Launching an interactive task in the operator's own terminal.

``tasks start`` hands the task to a ``claude`` process the operator watches,
which is a different concern from the ``tasks`` command surface itself: it owns
the argv (resume an existing session, or open a fresh one carrying the
interactive system context) and the in-place exec.
"""

import logging
import os
import pathlib
import shutil

from teatree.agents._headless_options import UUID_RE
from teatree.agents.prompt import build_interactive_context
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.core.models import Task
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.overlay_loader import get_overlay_for_ticket

logger = logging.getLogger(__name__)


def build_claude_command(task: Task) -> list[str]:
    """Build the ``claude`` argv for an interactive task.

    Resumes the prior session when the task carries a Claude session UUID,
    otherwise starts a fresh session with the interactive system context
    pre-loaded via ``--append-system-prompt``.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        msg = "claude CLI is not installed"
        raise FileNotFoundError(msg)

    agent_id = task.session.agent_id if task.session else ""
    if agent_id and UUID_RE.match(agent_id):
        logger.info("Resuming claude session %s for task %s", agent_id, task.pk)
        return [claude_bin, "--resume", agent_id]

    overlay_skill_metadata = get_overlay_for_ticket(task.ticket).metadata.get_skill_metadata()
    skills = resolve_skill_bundle(
        phase=task.phase,
        overlay_skill_metadata=overlay_skill_metadata,
        worktree_path=dispatch_worktree_path(task.ticket),
    )
    system_context = build_interactive_context(task, skills=skills)
    return [claude_bin, "--append-system-prompt", system_context]


def exec_inline(argv: list[str]) -> None:
    from teatree.utils.run import run_streamed  # noqa: PLC0415 — deferred: keeps command import light

    orig_cwd = os.environ.get("T3_ORIG_CWD", "")
    cwd = orig_cwd if orig_cwd and pathlib.Path(orig_cwd).is_dir() else None
    rc = run_streamed(argv, cwd=cwd, check=False)
    raise SystemExit(rc)
