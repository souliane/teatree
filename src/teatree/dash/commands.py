"""The allowlisted ``t3`` command surface for the health page (#3162).

The safe, phone-friendly debug tier: a fixed set of read-only-ish ``t3`` verbs run
as bounded subprocesses (timeout + captured output), never free-form shell and
never operator-supplied argv. The allowlist is CODE, not config — a new button is
a new :class:`CommandSpec` here, reviewed like any other code. Output is captured
and shown in the page (the poll-based UI has no long-lived stream), and every run
is audited by the caller.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_TIMEOUT_SECONDS = 120


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


def _argv(spec: CommandSpec, loop_name: str) -> tuple[str, ...]:
    if not spec.needs_loop:
        return spec.argv
    if not loop_name:
        msg = f"command {spec.key!r} requires a loop name"
        raise CommandNotAllowedError(msg)
    return (*spec.argv, loop_name)


def run_allowlisted(key: str, *, loop_name: str = "") -> CommandResult:
    """Run an allowlisted command as a bounded subprocess and capture its output.

    Refuses any key outside :data:`ALLOWED_COMMANDS`. A timeout is not an error —
    it returns a result flagged ``timed_out`` with the partial output, so a hung
    command shows in the page instead of hanging the request forever.
    """
    spec = ALLOWED_COMMANDS.get(key)
    if spec is None:
        msg = f"command {key!r} is not allowlisted"
        raise CommandNotAllowedError(msg)
    argv = _argv(spec, loop_name.strip())
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


def _decode(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def command_buttons() -> Sequence[CommandSpec]:
    """The allowlist as an ordered list for the template."""
    return tuple(ALLOWED_COMMANDS.values())
