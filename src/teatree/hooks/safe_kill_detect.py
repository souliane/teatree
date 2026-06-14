"""Detect a raw ``kill``/``kill -9`` of a process by pid — the PreToolUse safe-kill gate (#2225).

Pure command analysis (no ORM, no ``ps``) so the PreToolUse hook stays fast and
the detection is unit-testable. The hook denies a Bash command that signals a
process by a numeric pid and routes the agent to the deterministic
:func:`teatree.core.safe_kill.safe_kill` helper, which verifies positive
identity + confirmed non-live before signalling.

The agent's recurring mistake was killing the WRONG, LIVE ``claude`` process by
guessing which pid "looked dead". A raw ``kill <pid>`` / ``kill -9 <pid>`` /
``kill -SIGKILL <pid>`` is exactly that guessed-pid shape; it is denied so the
agent must go through the helper (positive session/task id + non-live proof)
instead.
"""

import re
from dataclasses import dataclass

# A ``kill`` invocation whose target is a bare numeric pid (optionally after a
# signal flag like ``-9`` / ``-KILL`` / ``-SIGKILL`` / ``-s SIGTERM``). A
# ``%job`` target, a ``$VAR`` target, or ``$(pgrep …)`` is NOT a raw-pid guess
# and is left alone.
_KILL_PID_RE = re.compile(
    r"""
    \bkill\b                      # the kill builtin/binary
    (?:\s+-s\s+\S+)?              # optional `-s SIGNAL`
    (?:\s+-[0-9A-Za-z]+)*          # optional signal flags (-9, -KILL, -SIGKILL, -TERM)
    (?:\s+--)?                     # optional end-of-options marker
    \s+
    (?P<pid>-?\d+)                # a bare numeric pid (a leading - = a process group)
    \b
    """,
    re.VERBOSE,
)

_PKILL_RE = re.compile(r"\b(?:pkill|killall)\b")

_SAFE_KILL_BLOCK_MSG = (
    "BLOCKED: this command signals a process by a raw pid. The agent has twice killed "
    "the WRONG, LIVE process by guessing which pid 'looked dead'. Before killing any "
    "process: (1) confirm the target by its session id (~/.claude/projects/*/<id>.jsonl) "
    "or task id with the user — never by 'looks idle'; (2) confirm it is non-live (two "
    "CPU samples with no activity AND a stated hang cause; a STAT of R/R+ or any +"
    " foreground state means it is running, not stuck). Route the kill through "
    "teatree.core.safe_kill.safe_kill(pid, hang_cause=...), which refuses unless both hold. "
    "A mid-action user interjection must abort."
)


@dataclass(frozen=True, slots=True)
class SafeKillDetection:
    """Whether a Bash command signals a process by raw pid, and the matched pid."""

    is_raw_pid_kill: bool
    pid: int | None
    message: str


def detect_raw_pid_kill(command: str) -> SafeKillDetection:
    """Return a detection for *command*; ``is_raw_pid_kill`` True iff it kills by raw pid.

    ``pkill``/``killall`` (signal by name, not pid) are NOT flagged here — they
    are a different surface. Only a literal ``kill <pid>`` numeric-pid target is
    the guessed-pid shape this gate refuses.
    """
    if not command:
        return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")
    if _PKILL_RE.search(command):
        return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")
    match = _KILL_PID_RE.search(command)
    if match is None:
        return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")
    try:
        pid = abs(int(match.group("pid")))
    except ValueError:
        return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")
    if pid <= 1:
        return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")
    return SafeKillDetection(is_raw_pid_kill=True, pid=pid, message=_SAFE_KILL_BLOCK_MSG)


__all__ = ["SafeKillDetection", "detect_raw_pid_kill"]
