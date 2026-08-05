"""EXERCISE teatree's own MCP server and say, concretely, why it is dead.

A registration that exists is not evidence that the server works. The night this
module was written, ``teatree`` was registered, ``claude mcp list`` had been printing
``✘ Failed to connect — -32000: Connection closed`` for a whole session, and the only
signal anywhere was one ``WARN`` line among ~twenty at ``SessionStart`` — most of the
others benign artifacts of the host/container boundary, so it scrolled past. The
detection existed and still cost hours, for two reasons this module fixes:

*   It was ADVISORY. An enabled-but-disconnected MCP server is not advice; it is the
    agent's structured view of teatree gone. The finding here is a hard FAIL.
*   It was DECLARATIVE. ``claude mcp list`` only ever says ``Connection closed`` — it
    never says why, and the three causes present identically:

    1.  a **stale tool env** — the installed dists drift behind ``pyproject.toml``
        and the server dies on ``ImportError`` before it writes a byte;
    2.  a **delegation failure** — the server serves in a domain that cannot open the
        control DB, and dies (or worse, answers ``initialize`` and then fails every
        ORM-backed tool call) on ``unable to open database file``;
    3.  a **startup over the handshake budget** — nothing is wrong except that the
        client gave up first.

So the check spawns the real server, speaks real stdio to it, and keeps the stderr
that ``claude mcp list`` throws away. Cause 3 is invisible to any check that does not
time the spawn; cause 2 is invisible to any check that stops at "did it answer",
because it DOES answer.

The classification (:func:`classify_exercise`) is a pure function over a captured run,
so every branch is reachable from a test with no subprocess at all — which is what
keeps this guard itself falsifiable.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.docker.workflow import is_running_in_container
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

#: A client that gives up before the server finishes booting reports only
#: ``Connection closed``, indistinguishable from a crash. Claude Code allows ~30s by
#: default, so this is deliberately tighter than the client's own patience: a startup
#: already past this is one contended box away from being intermittently dead, and the
#: whole point of the finding is to catch it BEFORE it presents as a mystery.
HANDSHAKE_BUDGET_SECONDS = 10.0

_INITIALIZE_FRAME = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "t3-doctor", "version": "1"},
        },
    },
)

_STALE_ENV_MARKERS = ("ImportError", "ModuleNotFoundError", "cannot import name")
_UNREACHABLE_DB_MARKERS = ("unable to open database file", "OperationalError")
_STDERR_EXCERPT_LINES = 12

_STALE_ENV_REMEDY = (
    "the installed dists have drifted behind pyproject.toml, so the server dies on "
    "import before it writes a byte. Repair the env that actually runs `t3`: "
    "`uv tool install --editable . --overrides uv-overrides.txt --reinstall` from the "
    "teatree clone (or `uv sync` for a project venv). Run it after EVERY update — this "
    "is the #4049 declared-versus-installed skew class."
)
_DELEGATION_REMEDY = (
    "the server is running in a domain that cannot open the control DB. That is a "
    "delegation failure: `teatree.cli.mcp_owning_domain.owning_domain_wrapper` must "
    "hand the server to the domain owning the DB, and it can only do so when it can "
    "SEE the claim. Confirm the containerized stack is up (`docker compose -f "
    "deploy/docker-compose.yml ps`) so the wrapper resolves, and check `t3 doctor "
    "check` for control-DB reachability. NOTE: this cause can present as CONNECTED — "
    "`initialize` needs no DB, so the handshake succeeds and every ORM-backed tool "
    "call fails afterwards."
)
_SLOW_STARTUP_REMEDY = (
    "nothing is broken except the clock: the client gives up first and reports only "
    "`Connection closed`. Profile with `python -X importtime -c 'import teatree.cli'` "
    "and keep `AppConfig.ready()` to registration — it runs inside every "
    "`django.setup()`, so a heavyweight SDK imported there is paid by every `t3` "
    "invocation."
)
_NO_RESPONSE_REMEDY = (
    "the server produced no usable `initialize` response and its stderr names none of "
    "the known causes. Reproduce it directly and read the whole trace: "
    "`echo '<initialize frame>' | t3 mcp serve`."
)


class McpFailureCause(StrEnum):
    """Why the exercised server is not usable — one cause, one concrete remedy."""

    STALE_TOOL_ENV = "stale-tool-env"
    DELEGATION_FAILURE = "delegation-failure"
    SLOW_STARTUP = "slow-startup"
    NO_RESPONSE = "no-response"

    @property
    def remedy(self) -> str:
        return {
            McpFailureCause.STALE_TOOL_ENV: _STALE_ENV_REMEDY,
            McpFailureCause.DELEGATION_FAILURE: _DELEGATION_REMEDY,
            McpFailureCause.SLOW_STARTUP: _SLOW_STARTUP_REMEDY,
            McpFailureCause.NO_RESPONSE: _NO_RESPONSE_REMEDY,
        }[self]


@dataclass(frozen=True, slots=True)
class ExerciseRun:
    """The raw result of spawning the server — the seam the tests inject at."""

    stdout: str
    stderr: str
    elapsed: float
    timed_out: bool


@dataclass(frozen=True, slots=True)
class McpExerciseOutcome:
    """Whether the exercised server is usable, and if not, why and what to do."""

    ok: bool
    elapsed: float
    cause: McpFailureCause | None = None
    stderr_excerpt: str = ""

    @property
    def finding(self) -> str:
        if self.ok:
            return f"teatree MCP server answered `initialize` in {self.elapsed:.1f}s."
        cause = self.cause
        if cause is None:  # pragma: no cover — a non-ok outcome always carries a cause
            return "teatree MCP server is NOT usable."
        return (
            f"teatree MCP server is NOT usable ({cause.value}, {self.elapsed:.1f}s) — "
            f"`claude mcp list` would report only `Connection closed`. {cause.remedy}"
        )


def _excerpt(stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if line.strip()]
    return "\n".join(lines[-_STDERR_EXCERPT_LINES:])


def _handshake_succeeded(stdout: str) -> bool:
    for line in stdout.splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if frame.get("id") == 1 and "result" in frame:
            return True
    return False


def _stderr_cause(stderr: str) -> McpFailureCause | None:
    if any(marker in stderr for marker in _STALE_ENV_MARKERS):
        return McpFailureCause.STALE_TOOL_ENV
    if any(marker in stderr for marker in _UNREACHABLE_DB_MARKERS):
        return McpFailureCause.DELEGATION_FAILURE
    return None


def classify_exercise(run: ExerciseRun, *, budget: float = HANDSHAKE_BUDGET_SECONDS) -> McpExerciseOutcome:
    """Turn a captured spawn into a verdict — pure, so every branch is testable.

    The order matters. A stale env is checked before an unreachable DB because a
    half-installed env produces both signatures and the import failure is the one to
    fix first. The DB signature is checked EVEN WHEN the handshake succeeded, because
    that is the shape the false green takes: ``initialize`` touches no ORM, so a server
    whose every real tool call fails still reports connected.
    """
    excerpt = _excerpt(run.stderr)
    if run.timed_out:
        return McpExerciseOutcome(
            ok=False,
            elapsed=run.elapsed,
            cause=_stderr_cause(run.stderr) or McpFailureCause.SLOW_STARTUP,
            stderr_excerpt=excerpt,
        )
    if not _handshake_succeeded(run.stdout):
        return McpExerciseOutcome(
            ok=False,
            elapsed=run.elapsed,
            cause=_stderr_cause(run.stderr) or McpFailureCause.NO_RESPONSE,
            stderr_excerpt=excerpt,
        )
    db_cause = _stderr_cause(run.stderr)
    if db_cause is not None:
        return McpExerciseOutcome(ok=False, elapsed=run.elapsed, cause=db_cause, stderr_excerpt=excerpt)
    if run.elapsed > budget:
        return McpExerciseOutcome(
            ok=False,
            elapsed=run.elapsed,
            cause=McpFailureCause.SLOW_STARTUP,
            stderr_excerpt=excerpt,
        )
    return McpExerciseOutcome(ok=True, elapsed=run.elapsed)


def spawn_mcp_server(argv: list[str], *, budget: float = HANDSHAKE_BUDGET_SECONDS) -> ExerciseRun:
    """Speak one ``initialize`` frame to *argv* over real stdio and capture everything.

    stdin is closed after the frame so the server reaches EOF and exits on its own
    rather than being killed mid-write — a killed server's stderr is the thing worth
    keeping, and terminating it can truncate exactly the traceback the operator needs.
    The timeout is the budget plus a margin, so a run that merely EXCEEDS the budget is
    still observed and reported as slow, not misreported as hung.
    """
    started = time.monotonic()
    try:
        completed = run_allowed_to_fail(
            argv,
            expected_codes=None,
            stdin_text=f"{_INITIALIZE_FRAME}\n",
            timeout=budget * 2,
        )
    except TimeoutExpired as expired:
        return ExerciseRun(
            stdout=_as_text(expired.stdout),
            stderr=_as_text(expired.stderr),
            elapsed=time.monotonic() - started,
            timed_out=True,
        )
    return ExerciseRun(
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed=time.monotonic() - started,
        timed_out=False,
    )


def _as_text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")


def exercise_mcp_server(
    argv: list[str],
    *,
    budget: float = HANDSHAKE_BUDGET_SECONDS,
    spawn: Callable[[list[str]], ExerciseRun] | None = None,
) -> McpExerciseOutcome:
    """Spawn the server, speak stdio to it, and classify what came back."""
    run = (spawn or (lambda command: spawn_mcp_server(command, budget=budget)))(argv)
    return classify_exercise(run, budget=budget)


def installed_side_label() -> str:
    """Which side of the host/container boundary this check's verdict describes.

    The two sides carry independent installs and drift independently — the host tool
    env sat three weeks behind ``pyproject.toml`` while the container was current — so
    a skew finding that does not name its side sends the operator to repair the wrong
    one.
    """
    return "the CONTAINER env" if is_running_in_container() else "the HOST tool env"


def skew_finding(skews: list[str], *, source: Path) -> str:
    return (
        f"{installed_side_label()} has drifted from {source}: "
        + "; ".join(skews)
        + ". A dep that is installed but too OLD is invisible to the missing-deps check "
        "and only ever surfaces as an ImportError at the moment something needs it "
        "(souliane/teatree#4049)."
    )
