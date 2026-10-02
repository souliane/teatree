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

Flagged regardless of how the program word is SPELLED: a path form
(``/bin/kill``), a transparent wrapper (``command``/``env``/``xargs``/…), an
env-assignment prefix, a compound-command keyword (``then kill 4242``) and a
``sudo`` elevation all name the same guessed pid, so the leader is canonicalised
before the compare.
"""

import re
from dataclasses import dataclass

from teatree.hooks._parser_primitives import canonical_leader, strip_wrapper_prefix
from teatree.hooks._shell_lexer import split_commands, tokenize

# A signal flag on ``kill``: ``-9`` / ``-KILL`` / ``-SIGKILL`` / ``-TERM`` / the
# explicit ``-s SIGNAL`` form. ``-0`` is matched here so it can be detected as
# the no-op probe and excluded.
_SIGNAL_FLAG_RE = re.compile(r"^-(?:s$|[0-9A-Za-z]+$)")

_PID_RE = re.compile(r"^-?\d+$")

# ``sudo`` stays out of the SHARED wrapper set (it is not privilege-transparent),
# so this gate strips it locally — an elevated kill reaches a process the agent
# could not otherwise signal.
# Only these ``sudo`` options consume a value token; every other flag is a switch.
_SUDO_VALUE_FLAGS = frozenset(
    {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-r", "--role", "-t", "--type"}
)

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


def _after_sudo_options(words: list[str]) -> list[str]:
    """The argv ``sudo`` elevates, past ``sudo``'s own options."""
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            return words[index + 1 :]
        if not word.startswith("-"):
            break
        index += 2 if word in _SUDO_VALUE_FLAGS else 1
    return words[index:]


def _kill_pid_in_words(words: list[str]) -> int | None:
    """Return the raw pid a ``kill`` segment targets, or ``None`` when it is not one.

    The segment's executed program must be ``kill``, resolved the same way every
    other leak/publish detector resolves a leader: a leading env-assignment run, a
    ``cd``/``pushd`` pair, a compound-command keyword and ONE transparent wrapper
    are stripped (:func:`strip_wrapper_prefix`), then the basename is taken
    (:func:`canonical_leader`). A verbatim ``words[0] == "kill"`` test let
    ``/bin/kill 4242``, ``command kill 4242``, ``env kill 4242`` and ``then kill
    4242`` name the same guessed pid while passing as "not a kill". A leading
    ``sudo`` and its own options are dropped for the same reason
    (:func:`_after_sudo_options`). A non-numeric target (``%job``, ``$VAR``,
    ``$(…)``), a no-op ``-0``/``-s 0`` probe, and a segment whose program is not
    ``kill`` all yield ``None``.
    """
    program_words = strip_wrapper_prefix(words)
    if program_words and canonical_leader(program_words[0]) == "sudo":
        program_words = strip_wrapper_prefix(_after_sudo_options(program_words))
    if not program_words or canonical_leader(program_words[0]) != "kill":
        return None
    index = _operand_index(program_words)
    if index is None or index >= len(program_words):
        return None
    target = program_words[index]
    if not _PID_RE.match(target):
        return None  # %job / $VAR / $(…) / a flag — not a raw numeric pid
    pid = abs(int(target))
    return pid if pid > 1 else None


def detect_raw_pid_kill(command: str) -> SafeKillDetection:
    """Return a detection for *command*; ``is_raw_pid_kill`` True iff it kills by raw pid.

    Detection is anchored to a command position via the shared quote-accurate
    shell lexer (:mod:`teatree.hooks._shell_lexer`): each ``;``/``&&``/``||``/
    ``|``/newline-separated segment whose executed PROGRAM is ``kill`` is
    inspected (:func:`_kill_pid_in_words` canonicalises the leader), so
    a ``kill`` token inside a quoted string (``echo "if it hangs; kill 1234"``),
    a comment, or as another command's argument is never split apart into a bogus
    kill segment. ``kill -0`` (no-op probe), ``pkill``/``killall`` (signal by
    name), and ``%job``/``$VAR``/``$(…)`` targets are left alone.
    """
    if not command:
        return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")
    for segment in split_commands(tokenize(command)):
        pid = _kill_pid_in_words([tok.value for tok in segment])
        if pid is not None:
            return SafeKillDetection(is_raw_pid_kill=True, pid=pid, message=_SAFE_KILL_BLOCK_MSG)
    return SafeKillDetection(is_raw_pid_kill=False, pid=None, message="")


__all__ = ["SafeKillDetection", "detect_raw_pid_kill"]
