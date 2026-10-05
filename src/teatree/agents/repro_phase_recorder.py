"""Require pre-fix RED evidence and run its GREEN command in the test phase."""

import os
import shlex

from teatree.core.gates.fix_dod_gate import is_fix
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task
from teatree.core.models.repro_evidence import HarnessRun, ReproEvidence, ReproEvidenceError
from teatree.core.models.repro_waiver import ReproWaiver
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.utils import git
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_bounded_group

GREEN_REPLAY_TIMEOUT_SECONDS = 120
GREEN_REPLAY_OUTPUT_MAX_BYTES = 64 * 1024
GREEN_REPLAY_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "TMPDIR"})
GREEN_REPLAY_UV_ENV_KEYS = frozenset(
    {
        "UV_CACHE_DIR",
        "UV_LINK_MODE",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_PYTHON_DOWNLOADS",
        "UV_PROJECT_ENVIRONMENT",
        "UV_NO_SYNC",
        "UV_OFFLINE",
    }
)


def _bounded_output(stdout: str, stderr: str = "", *, header: str = "") -> str:
    """*header*, then the TAIL of stdout and of stderr, within the byte cap — a test run summarises at its end.

    Each stream keeps at least half of the room, and a stream that needs less leaves the rest to the
    other, so a flood of stderr never pushes the stdout summary out.
    """
    room = max(GREEN_REPLAY_OUTPUT_MAX_BYTES - len(header.encode("utf-8")), 0)
    out = stdout.encode("utf-8", errors="replace")
    err = stderr.encode("utf-8", errors="replace")
    out_room = min(len(out), max(room // 2, room - len(err)))
    return header + _tail(out, out_room) + _tail(err, room - out_room)


def _tail(encoded: bytes, room: int) -> str:
    """The last *room* bytes of *encoded*, a character cut at the start dropped rather than replaced."""
    return encoded[len(encoded) - min(room, len(encoded)) :].decode("utf-8", errors="ignore")


def _run_green_replay(
    command: str, workdir: str, head_sha: str, *, timeout: float = GREEN_REPLAY_TIMEOUT_SECONDS
) -> HarnessRun:
    """Execute a bounded replay without passing worker credentials to the command."""
    allowed = GREEN_REPLAY_ENV_KEYS | GREEN_REPLAY_UV_ENV_KEYS
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    script = f"cd -- {shlex.quote(str(workdir))} && {command}"
    try:
        result = run_bounded_group(["bash", "-c", script], expected_codes=None, env=environment, timeout=timeout)
    except TimeoutExpired as exc:
        stdout, stderr = (
            part.decode(errors="replace") if isinstance(part, bytes) else part
            for part in (exc.stdout or "", exc.stderr or "")
        )
        return HarnessRun(
            head_sha=head_sha,
            exit_code=124,
            output=_bounded_output(stdout, stderr, header=f"GREEN replay timed out after {timeout}s\n"),
        )
    except OSError as exc:
        return HarnessRun(
            head_sha=head_sha, exit_code=127, output=_bounded_output(f"GREEN replay could not start: {exc}")
        )
    return HarnessRun(
        head_sha=head_sha, exit_code=result.returncode, output=_bounded_output(result.stdout, result.stderr)
    )


def record_phase_repro(task: Task, *, phase: str) -> str:
    """Return a refusal until a FIX has a real RED, then record GREEN at test completion."""
    canonical = normalize_phase(phase or task.phase)
    ticket = task.ticket
    if (
        canonical not in {"coding", "testing"}
        or not is_fix(ticket)
        or ReproWaiver.objects.filter(ticket=ticket).exists()
        or ReproEvidence.objects.has_valid_repro(ticket)
    ):
        return ""
    red = ReproEvidence.objects.filter(ticket=ticket).exclude(red_exit_code=0).order_by("-red_recorded_at").first()
    if red is None:
        return (
            "executed repro missing: run `repro record-red` against the pre-fix HEAD before coding completes; "
            "the test phase will execute and record the matching GREEN run"
        )
    if canonical == "coding":
        return ""
    return _record_green(task, red)


def _record_green(task: Task, red: ReproEvidence) -> str:
    """Replay the RED command and persist the guarded GREEN artifact."""
    workdir = dispatch_worktree_path(task.ticket)
    if not workdir:
        return "executed repro GREEN cannot run: ticket has no dispatch worktree"
    try:
        green_sha = git.head_sha(repo=workdir)
    except CommandFailedError as exc:
        return f"executed repro GREEN cannot read worktree head SHA: {exc}"
    run = _run_green_replay(red.command, workdir, green_sha)
    if run.exit_code != 0:
        try:
            ReproEvidence.record_failed_green(ticket=task.ticket, command=red.command, run=run)
        except ReproEvidenceError as exc:
            return f"executed repro GREEN refused: {exc}"
        return f"executed repro GREEN failed (exit {run.exit_code}): {run.output[-4000:]}"
    ancestor = git.check(repo=workdir, args=["merge-base", "--is-ancestor", red.red_head_sha, green_sha])
    try:
        ReproEvidence.record_green(ticket=task.ticket, command=red.command, run=run, red_is_ancestor=ancestor)
    except ReproEvidenceError as exc:
        return f"executed repro GREEN refused: {exc}"
    return ""
