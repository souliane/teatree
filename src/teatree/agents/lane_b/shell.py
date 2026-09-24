"""Shell capability — a denylist/timeout-guarded command runner exposed as ``Bash``.

A single tool on a teatree-owned ``FunctionToolset``, exposed to the model under the
skill/SDK name ``Bash`` (:mod:`teatree.agents.lane_b.tool_names`). The coarse
denylist + per-command timeout live here; the AUTHORITATIVE parity surface is the
shared hard-deny gate registry (:mod:`teatree.agents.lane_b.gating`), which wraps
this toolset and is consulted for the exact same set of refusals Lane A's
PreToolUse hook enforces. This module's denylist is only a cheap first cut so an
obviously-destructive command is refused even before the gate wrapper runs.
"""

import shutil
from pathlib import Path

from pydantic_ai.toolsets.function import FunctionToolset

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.tool_errors import ToolInputError
from teatree.agents.lane_b.tool_names import TOOL_BASH
from teatree.utils.run import TimeoutExpired, redact_secrets, run_allowed_to_fail


class ShellDeniedError(ToolInputError, RuntimeError):
    """A command matched the coarse Shell denylist — refused before execution."""


class ShellTimeoutError(ToolInputError, RuntimeError):
    """A command exceeded its per-call wall-clock ceiling — a ``ToolInputError``.

    The model chose (or was handed) a command that ran too long — same family as a
    denied path or a bad substring: it can retry narrower or backgrounded, so this
    is retryable, not a teatree defect. Before this existed, ``subprocess.TimeoutExpired``
    escaped uncaught (not a member of
    :data:`~teatree.agents.lane_b.tool_errors.CORRECTABLE_TOOL_ERRORS`) and crashed the
    whole dispatch — the repeated ``harness_crash`` on unbounded filesystem-wide
    ``find /`` searches was this, three times over.
    """


def _denylisted(command: str, denylist: tuple[str, ...]) -> str | None:
    """Return the matched denylist entry, or ``None`` when the command is clear."""
    normalized = " ".join(command.split())
    return next((entry for entry in denylist if entry in normalized), None)


def _elision_marker(dropped: int) -> str:
    """The head/tail seam naming the dropped bytes and how to get them back."""
    return (
        f"\n[…{dropped} bytes elided from the middle of this output — "
        f"re-run the command narrowed (grep/head/tail/--stat) to obtain them]\n"
    )


def _capped(output: str, max_bytes: int) -> str:
    """Return *output* bounded to *max_bytes* as head + marker + tail; ``0`` disables the cap.

    An oversized return is re-sent on every later request of the thread, so it is
    the per-request cost this bounds — not the command, which always ran to
    completion and is never failed on its output size. Head and tail are both kept
    because a long run's first lines (what it was doing) and last lines (how it
    ended) are the load-bearing ones. The marker is sized against the whole-output
    worst case, so the result never exceeds ``max(max_bytes, marker length)`` — below
    the marker length only the marker is returned — and the byte slices are decoded
    with ``errors="ignore"`` so a cut never splits a codepoint.
    """
    encoded = output.encode()
    if max_bytes <= 0 or len(encoded) <= max_bytes:
        return output
    budget = max(0, max_bytes - len(_elision_marker(len(encoded)).encode()))
    head = encoded[: budget // 2].decode(errors="ignore")
    tail = encoded[len(encoded) - (budget - budget // 2) :].decode(errors="ignore") if budget else ""
    return f"{head}{_elision_marker(len(encoded) - len(head.encode()) - len(tail.encode()))}{tail}"


def _resolve_shell() -> str:
    """Resolve an ABSOLUTE path to a POSIX ``-c`` shell, preferring ``bash`` (#3157 AH-11).

    ``["bash", "-c", cmd]`` assumed ``bash`` sits first on ``PATH`` — a bare-name
    assumption inside the OS-agnostic tool constraint that breaks when ``bash`` is
    installed at a non-first location (Homebrew ``/opt/homebrew/bin``) or ``PATH`` is
    unusual. Resolving the absolute path via :func:`shutil.which` documents the
    POSIX-shell requirement portably and keeps the runner working regardless of
    ``PATH`` ordering. Falls back to POSIX ``sh`` (pipes/redirects the tool relies on
    are POSIX-portable), and finally to the bare ``"bash"`` name so the runner still
    surfaces a clear "not found" rather than silently mis-resolving.
    """
    return shutil.which("bash") or shutil.which("sh") or "bash"


def build_shell_toolset(config: LaneBToolConfig) -> FunctionToolset[None]:
    """Assemble the Shell ``FunctionToolset`` bound to *config*'s knobs.

    The command runs with ``cwd`` pinned to the worktree root (or the process cwd
    when the task has none), under *config*'s pinned child env, with the
    per-command timeout enforced by the shared ``teatree.utils.run`` wrapper. A
    denylist hit raises :class:`ShellDeniedError`; a timeout raises
    :class:`ShellTimeoutError` — both ``ToolInputError`` — so the gate wrapper
    (:class:`~teatree.agents.lane_b.gating.HardDenyToolset`) turns either into a
    retryable ``ModelRetry`` instead of crashing the run. The command is passed
    through a resolved POSIX ``-c`` shell
    (:func:`_resolve_shell`, absolute path preferring ``bash``) so the list-based
    runner still evaluates a full shell string (pipes, redirects) — the runner is
    the sanctioned chokepoint, not raw ``subprocess``.

    The combined output is bounded by :func:`_capped` to
    ``config.shell_max_output_bytes`` before it is returned. That bounds what every
    LATER request of the thread re-sends; the command itself always runs to
    completion and is never failed on its output size.
    """
    toolset: FunctionToolset[None] = FunctionToolset()
    cwd = str(config.fs_root) if config.fs_root else str(Path.cwd())
    shell_bin = _resolve_shell()

    def shell(command: str) -> str:
        """Run a shell command in the worktree and return its combined output."""
        denied = _denylisted(command, config.shell_denylist)
        if denied is not None:
            msg = f"command refused: matches Shell denylist entry {denied!r}"
            raise ShellDeniedError(msg)
        # ``expected_codes=None`` accepts any exit code — the tool REPORTS the
        # exit status to the model rather than raising on a non-zero one.
        try:
            result = run_allowed_to_fail(
                [shell_bin, "-c", command],
                expected_codes=None,
                env=config.shell_env or None,
                cwd=cwd,
                timeout=config.shell_timeout_seconds,
            )
        except TimeoutExpired as exc:
            # Redacted like ``CommandFailedError`` — this text can end up in a
            # TaskAttempt.error record, not just the model's own turn.
            msg = f"command timed out after {exc.timeout}s: {redact_secrets(command)}"
            raise ShellTimeoutError(msg) from exc
        return f"exit={result.returncode}\n{_capped(f'{result.stdout}{result.stderr}', config.shell_max_output_bytes)}"

    # Exposed to the model as ``Bash`` (the skill/SDK vocabulary) so a skill saying
    # ``Bash`` names this tool; the pythonic ``shell`` function name stays local.
    toolset.add_function(shell, takes_ctx=False, name=TOOL_BASH)
    return toolset
