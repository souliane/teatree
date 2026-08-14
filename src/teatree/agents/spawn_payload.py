"""Measure and name the ``execve`` payload a CLI-child spawn must fit — the E2BIG boundary.

A harness that spawns the bundled ``claude`` CLI hands the kernel an argument
vector and an environment; ``execve`` refuses both together past a limit and the
only symptom is ``OSError: [Errno 7] Argument list too long``, surfaced by the SDK
as a ``CLIConnectionError`` whose forty frames say nothing about size. The failure
is a pure function of payload size, so it repeats identically on every retry and
reads as a ticket defect while no ticket content is implicated.

Two DIFFERENT kernel limits apply, and a check against one never sees the other:
``ARG_MAX`` bounds argv+envp TOGETHER (``sysconf(_SC_ARG_MAX)``, on Linux the
stack rlimit / 4), while ``MAX_ARG_STRLEN`` caps any SINGLE argument at 32 pages
regardless of how small the total is. :func:`measure_spawn_payload` reports both
so a caller can say which one it is about to breach, and
:func:`spawn_refusal_reason` turns the measurement into the named refusal the
operator gets instead of an errno.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: Linux caps a single argv/envp string at 32 pages, whatever ``ARG_MAX`` allows.
MAX_ARG_STRLEN = 32 * 4096
#: Used when ``sysconf`` cannot answer — the kernel's own ``ARG_MAX`` floor, so an
#: unmeasurable host is judged against the smallest limit it could really have.
_FALLBACK_ARG_MAX = MAX_ARG_STRLEN
#: ``execve`` charges each string a terminating NUL and a pointer in the new stack.
_PER_STRING_OVERHEAD = 1 + 8

E2BIG_PHRASES = ("[errno 7]", "argument list too long")


class AgentSpawnError(RuntimeError):
    """The agent process could NOT START — no work was attempted.

    Raised in place of the transport's bare errno so every downstream reader (the
    attempt recorder, the repair-halt escalation, the operator) can tell "the agent
    never started" apart from "the agent ran and the work failed".
    """


def arg_max_bytes() -> int:
    """The host's total argv+envp limit in bytes, or the kernel floor when unreadable."""
    try:
        limit = os.sysconf("SC_ARG_MAX")
    except (OSError, ValueError):
        return _FALLBACK_ARG_MAX
    return limit if limit > 0 else _FALLBACK_ARG_MAX


@dataclass(frozen=True, slots=True)
class SpawnPayload:
    """What one ``execve`` would charge against the two kernel limits.

    ``argv_bytes`` is a FLOOR when the caller can only account for the arguments it
    owns — every consumer is written to be sound under that reading (a floor over the
    limit is over the limit; a floor under it refuses nothing).
    """

    argv_bytes: int
    env_bytes: int
    largest_arg_bytes: int
    total_limit_bytes: int
    arg_limit_bytes: int = MAX_ARG_STRLEN

    @property
    def total_bytes(self) -> int:
        return self.argv_bytes + self.env_bytes

    @property
    def headroom_bytes(self) -> int:
        return self.total_limit_bytes - self.total_bytes

    @property
    def used_percent(self) -> float:
        return 100.0 * self.total_bytes / self.total_limit_bytes if self.total_limit_bytes else 0.0

    def gauge(self) -> str:
        """One line an operator can read as a trend rather than a cliff."""
        return (
            f"spawn payload at least {self.total_bytes} B of {self.total_limit_bytes} B "
            f"({self.used_percent:.1f}% used, {self.headroom_bytes} B headroom): "
            f"argv {self.argv_bytes} B, env {self.env_bytes} B, "
            f"largest argument {self.largest_arg_bytes} B of {self.arg_limit_bytes} B"
        )


def measure_spawn_payload(argv: Sequence[str], env: Mapping[str, str]) -> SpawnPayload:
    """Charge *argv* and *env* the way ``execve`` does, against this host's limits."""
    arg_sizes = [len(arg.encode()) for arg in argv]
    return SpawnPayload(
        argv_bytes=sum(size + _PER_STRING_OVERHEAD for size in arg_sizes),
        env_bytes=sum(len(f"{key}={value}".encode()) + _PER_STRING_OVERHEAD for key, value in env.items()),
        largest_arg_bytes=max(arg_sizes, default=0),
        total_limit_bytes=arg_max_bytes(),
    )


def spawn_refusal_reason(payload: SpawnPayload) -> str:
    """The named refusal for an unspawnable *payload*, or ``""`` when it fits.

    Names the breached limit and the measured size, because "argument list too
    long" alone tells the operator neither which of the two limits was hit nor by
    how much — and the two have entirely different fixes (shrink the environment
    versus move a single large argument off argv).
    """
    if payload.largest_arg_bytes > payload.arg_limit_bytes:
        return (
            f"a single spawn argument is {payload.largest_arg_bytes} bytes, over this platform's "
            f"{payload.arg_limit_bytes}-byte per-argument limit (E2BIG)"
        )
    if payload.total_bytes > payload.total_limit_bytes:
        return (
            f"the spawn payload is {payload.total_bytes} bytes (argv {payload.argv_bytes} + "
            f"env {payload.env_bytes}), over this platform's {payload.total_limit_bytes}-byte "
            f"argv+env limit (E2BIG)"
        )
    return ""


def is_e2big(text: str) -> bool:
    """Whether *text* carries an ``E2BIG`` spawn failure, however deeply nested."""
    haystack = text.casefold()
    return any(phrase in haystack for phrase in E2BIG_PHRASES)


def e2big_message(payload: SpawnPayload) -> str:
    """The operator-facing sentence for an E2BIG spawn, measured rather than guessed.

    States the non-implication explicitly: an E2BIG death happens before the child
    reads a single byte of the task, so adjudicating the ticket cannot help.
    """
    reason = spawn_refusal_reason(payload) or (
        f"the spawn payload measured {payload.total_bytes} bytes against a "
        f"{payload.total_limit_bytes}-byte limit, which the kernel nonetheless refused (E2BIG)"
    )
    return (
        f"agent could not be spawned: {reason}. The agent never started, so no work was attempted "
        f"and nothing about the ticket's content is implicated. {payload.gauge()}"
    )
