"""The allowlisted ``t3`` command surface for the health page (#3162).

The safe, phone-friendly debug tier: a fixed set of read-only-ish ``t3`` verbs run
as bounded subprocesses (timeout + captured output), never free-form shell and
never operator-supplied argv. The allowlist is CODE, not config — a new button is
a new :class:`CommandSpec` here, reviewed like any other code. Output is captured
and shown in the page (the poll-based UI has no long-lived stream), and every run
is audited by the caller.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from teatree.core.models.loop import Loop
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_TIMEOUT_SECONDS = 120
#: A command run blocks its worker thread for up to :data:`_TIMEOUT_SECONDS`. The
#: dashboard serves from one gunicorn worker with a small thread pool, so unbounded
#: concurrent runs — a few buttons clicked at once, or one hung command re-clicked —
#: would occupy every thread and freeze the page. Capping concurrent runs below the
#: pool size guarantees threads stay free to serve the dashboard while a slow
#: command is in flight.
_MAX_CONCURRENT_RUNS = 2


@dataclass(frozen=True, slots=True)
class CommandSpec:
    key: str
    label: str
    argv: tuple[str, ...]
    needs_loop: bool = False


# Fixed allowlist. Each argv is a top-level `t3` verb that is read-only or a
# single per-loop tick — nothing that mutates shared history or takes free input.
ALLOWED_COMMANDS: dict[str, CommandSpec] = {
    "doctor": CommandSpec(key="doctor", label="t3 doctor check", argv=("t3", "doctor", "check")),
    "worker-status": CommandSpec(key="worker-status", label="t3 worker status", argv=("t3", "worker", "status")),
    "loops-list": CommandSpec(key="loops-list", label="t3 loops list", argv=("t3", "loops", "list")),
    "loop-tick": CommandSpec(
        key="loop-tick",
        label="t3 loops tick --loop <name>",
        argv=("t3", "loops", "tick", "--loop"),
        needs_loop=True,
    ),
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    key: str
    label: str
    argv: tuple[str, ...]
    exit_code: int
    output: str
    timed_out: bool


class CommandNotAllowedError(ValueError):
    """A command-run POST named a key not in the fixed allowlist."""


class CommandBusyError(RuntimeError):
    """A command-run POST arrived while the same command — or the concurrency cap — was in flight."""


class _RunGuard:
    """In-process guard that bounds concurrent allowlisted runs (per worker process).

    Two overlapping protections against worker-thread starvation: a key already
    in flight is deduped (a re-clicked hung command never stacks a second run),
    and the total number of concurrent runs is capped below the worker's thread
    pool so a slow command can never occupy every thread.
    """

    def __init__(self, *, max_concurrent: int) -> None:
        self._max_concurrent = max_concurrent
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def acquire(self, key: str) -> None:
        with self._lock:
            if key in self._in_flight:
                msg = f"command {key!r} is already running — wait for it to finish"
                raise CommandBusyError(msg)
            if len(self._in_flight) >= self._max_concurrent:
                msg = "too many commands running — wait for one to finish"
                raise CommandBusyError(msg)
            self._in_flight.add(key)

    def release(self, key: str) -> None:
        with self._lock:
            self._in_flight.discard(key)


_run_guard = _RunGuard(max_concurrent=_MAX_CONCURRENT_RUNS)


def _argv(spec: CommandSpec, loop_name: str) -> tuple[str, ...]:
    if not spec.needs_loop:
        return spec.argv
    if not loop_name:
        msg = f"command {spec.key!r} requires a loop name"
        raise CommandNotAllowedError(msg)
    # Validate against registered loops before it reaches argv — the value is
    # operator-supplied POST data, and only a real Loop name is a legal target.
    if not Loop.objects.filter(name=loop_name).exists():
        msg = f"unknown loop {loop_name!r}"
        raise CommandNotAllowedError(msg)
    return (*spec.argv, loop_name)


def run_allowlisted(key: str, *, loop_name: str = "") -> CommandResult:
    """Run an allowlisted command as a bounded subprocess and capture its output.

    Refuses any key outside :data:`ALLOWED_COMMANDS`. A timeout is not an error —
    it returns a result flagged ``timed_out`` with the partial output, so a hung
    command shows in the page instead of hanging the request forever. Raises
    :class:`CommandBusyError` when the same command is already in flight or the
    concurrency cap is reached, so a slow command cannot starve the worker threads.
    """
    spec = ALLOWED_COMMANDS.get(key)
    if spec is None:
        msg = f"command {key!r} is not allowlisted"
        raise CommandNotAllowedError(msg)
    argv = _argv(spec, loop_name.strip())
    _run_guard.acquire(key)
    try:
        try:
            result = run_allowed_to_fail(list(argv), expected_codes=None, timeout=_TIMEOUT_SECONDS)
        except TimeoutExpired as exc:
            return CommandResult(
                key=key,
                label=spec.label,
                argv=argv,
                exit_code=-1,
                output=_decode(exc.output) + _decode(exc.stderr),
                timed_out=True,
            )
        return CommandResult(
            key=key,
            label=spec.label,
            argv=argv,
            exit_code=result.returncode,
            output=(result.stdout or "") + (result.stderr or ""),
            timed_out=False,
        )
    finally:
        _run_guard.release(key)


def _decode(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def command_buttons() -> Sequence[CommandSpec]:
    """The allowlist as an ordered list for the template."""
    return tuple(ALLOWED_COMMANDS.values())
