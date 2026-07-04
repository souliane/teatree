"""Shell capability — a denylist/timeout-guarded command runner (Bash parity).

A single ``shell`` tool on a teatree-owned ``FunctionToolset``. The coarse
denylist + per-command timeout live here; the AUTHORITATIVE parity surface is the
shared hard-deny gate registry (:mod:`teatree.agents.lane_b.gating`), which wraps
this toolset and is consulted for the exact same set of refusals Lane A's
PreToolUse hook enforces. This module's denylist is only a cheap first cut so an
obviously-destructive command is refused even before the gate wrapper runs.
"""

from pathlib import Path

from pydantic_ai.toolsets.function import FunctionToolset

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.utils.run import run_allowed_to_fail


class ShellDeniedError(RuntimeError):
    """A command matched the coarse Shell denylist — refused before execution."""


def _denylisted(command: str, denylist: tuple[str, ...]) -> str | None:
    """Return the matched denylist entry, or ``None`` when the command is clear."""
    normalized = " ".join(command.split())
    return next((entry for entry in denylist if entry in normalized), None)


def build_shell_toolset(config: LaneBToolConfig) -> FunctionToolset[None]:
    """Assemble the Shell ``FunctionToolset`` bound to *config*'s knobs.

    The command runs with ``cwd`` pinned to the worktree root (or the process cwd
    when the task has none), under *config*'s pinned child env, with the
    per-command timeout enforced by the shared ``teatree.utils.run`` wrapper. A
    denylist hit raises :class:`ShellDeniedError`; a timeout raises the tool error
    the model sees. The command is passed through ``bash -c`` so the list-based
    runner still evaluates a full shell string (pipes, redirects) — the runner is
    the sanctioned chokepoint, not raw ``subprocess``.
    """
    toolset: FunctionToolset[None] = FunctionToolset()
    cwd = str(config.fs_root) if config.fs_root else str(Path.cwd())

    def shell(command: str) -> str:
        """Run a shell command in the worktree and return its combined output."""
        denied = _denylisted(command, config.shell_denylist)
        if denied is not None:
            msg = f"command refused: matches Shell denylist entry {denied!r}"
            raise ShellDeniedError(msg)
        # ``expected_codes=None`` accepts any exit code — the tool REPORTS the
        # exit status to the model rather than raising on a non-zero one.
        result = run_allowed_to_fail(
            ["bash", "-c", command],
            expected_codes=None,
            env=config.shell_env or None,
            cwd=cwd,
            timeout=config.shell_timeout_seconds,
        )
        return f"exit={result.returncode}\n{result.stdout}{result.stderr}"

    toolset.add_function(shell, takes_ctx=False)
    return toolset
