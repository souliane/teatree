"""Detect a raw ``kill``/``kill -9`` of a process by pid — the PreToolUse safe-kill gate (#2225).

Pure command analysis (no ORM, no ``ps``) so the PreToolUse hook stays fast and
the detection is unit-testable. The hook denies a Bash command that signals a
process by a numeric pid and routes the agent to the runnable
``t3 teatree safe-kill <pid> --hang-cause "<why>"`` CLI, which verifies positive
identity + confirmed non-live before signalling.

The agent's recurring mistake was killing the WRONG, LIVE ``claude`` process by
guessing which pid "looked dead". A raw ``kill <pid>`` / ``kill -9 <pid>`` /
``kill -SIGKILL <pid>`` is exactly that guessed-pid shape; it is denied so the
agent must go through the CLI (positive session/task id + non-live proof)
instead.

Deliberately NOT flagged. ``kill -0 <pid>`` (and ``kill -s 0``) — signal 0 sends
no signal; it is the canonical no-op liveness probe (the codebase's own
``os.kill(pid, 0)`` pattern). ``pkill`` / ``killall`` — signal by name, a
different surface. A ``%job`` / ``$VAR`` / ``$(pgrep …)`` target — not a raw
numeric-pid guess. A ``kill`` token that is NOT at a command position — inside a
comment (``# kill 4242``), inside a string (``echo "to kill: kill 1234"``), as
another command's argument (``grep kill 4242 file``), or as a subcommand word
(``git kill 5``).
"""

import re
from dataclasses import dataclass

# Command-position anchor: a segment starts at the beginning of the command, or
# right after a shell separator (``;`` ``&&`` ``||`` ``|`` ``&`` newline). The
# first WORD of a segment is the command name; only when that word is exactly
# ``kill`` is the segment a kill invocation.
_SEGMENT_SPLIT_RE = re.compile(r"(?:\|\||&&|[;|&\n])")

# A signal flag on ``kill``: ``-9`` / ``-KILL`` / ``-SIGKILL`` / ``-TERM`` / the
# explicit ``-s SIGNAL`` form. ``-0`` is matched here so it can be detected as
# the no-op probe and excluded.
_SIGNAL_FLAG_RE = re.compile(r"^-(?:s$|[0-9A-Za-z]+$)")

_PID_RE = re.compile(r"^-?\d+$")

_SAFE_KILL_BLOCK_MSG = (
    "BLOCKED: this command signals a process by a raw pid. The agent has twice killed "
    "the WRONG, LIVE process by guessing which pid 'looked dead'. Before killing any "
    "process: (1) confirm the target by its session id (~/.claude/sessions/*.json maps "
    "pid->sessionId) or task id with the user — never by 'looks idle'; (2) confirm it is "
    "non-live (two CPU samples with no activity AND a stated hang cause; a STAT of R/R+ "
    "or any + foreground state means it is running, not stuck). Run "
    '`t3 teatree safe-kill <pid> --hang-cause "<why>"` instead — it refuses unless both '
    "hold. A mid-action user interjection must abort."
)


@dataclass(frozen=True, slots=True)
class SafeKillDetection:
    """Whether a Bash command signals a process by raw pid, and the matched pid."""

    is_raw_pid_kill: bool
    pid: int | None
    message: str


def _operand_index(words: list[str]) -> int | None:
    """Index of the first non-flag operand after ``kill``, or ``None`` for a no-op probe.

    Leading signal flags (``-9``, ``-SIGKILL``, ``-s TERM``, ``--``) are consumed
    so the operand is the target, not the signal. ``-0`` / ``-s 0`` are the no-op
    liveness probe (``None``) — they send no signal and must not be flagged.
    """
    i = 1
    while i < len(words):
        word = words[i]
        if word == "--":
            return i + 1
        if word == "-0":
            return None
        if word == "-s":
            if i + 1 < len(words) and words[i + 1] == "0":
                return None
            i += 2  # `-s SIGNAL` consumes two tokens
            continue
        if _SIGNAL_FLAG_RE.match(word):
            i += 1
            continue
        return i
    return i


def _kill_pid_in_segment(segment: str) -> int | None:
    """Return the raw pid a ``kill`` segment targets, or ``None`` when it is not one.

    The segment's first word must be exactly ``kill``. A non-numeric target
    (``%job``, ``$VAR``, ``$(…)``), a no-op ``-0``/``-s 0`` probe, and a ``kill``
    that is not the command name all yield ``None``.
    """
    words = segment.split()
    if not words or words[0] != "kill":
        return None
    index = _operand_index(words)
    if index is None or index >= len(words):
        return None
    target = words[index]
    if not _PID_RE.match(target):
        return None  # %job / $VAR / $(…) / a flag — not a raw numeric pid
    pid = abs(int(target))
    return pid if pid > 1 else None


def detect_raw_pid_kill(command: str) -> SafeKillDetection:
    """Return a detection for *command*; ``is_raw_pid_kill`` True iff it kills by raw pid.

    Detection is anchored to a command position: each ``;``/``&&``/``||``/``|``/
    newline-separated segment whose first word is ``kill`` is inspected, so a
    ``kill`` token inside a comment, string, or as another command's argument is
    not flagged. ``kill -0`` (no-op probe), ``pkill``/``killall`` (signal by
    name), and ``%job``/``$VAR``/``$(…)`` targets are left alone.
    """
    if not command:
        return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")
    for segment in _SEGMENT_SPLIT_RE.split(command):
        pid = _kill_pid_in_segment(segment.strip())
        if pid is not None:
            return SafeKillDetection(is_raw_pid_kill=True, pid=pid, message=_SAFE_KILL_BLOCK_MSG)
    return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")


__all__ = ["SafeKillDetection", "detect_raw_pid_kill"]
