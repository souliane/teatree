"""Typed subprocess wrappers.

Every ``subprocess.run`` / ``subprocess.Popen`` call in ``src/teatree`` MUST go
through these wrappers.  Raw subprocess usage is the ``subprocess-egress``
chokepoint in ``src/teatree/quality/chokepoints.yaml``, enforced by the
``check-chokepoints`` prek hook (see ``scripts/hooks/check_chokepoints.py``).
Every ``git`` argv leaves pinned by :class:`~teatree.utils.git_pins.GitPins`, so a ``core.fsmonitor`` /
``core.hooksPath`` planted in a checkout's config cannot run in the control plane.

Three entry points:

- ``run_checked`` — raises ``CommandFailedError`` on non-zero.  Use for
    infrastructure calls where failure is a bug: ``createdb``, ``dropdb``,
    ``docker compose``, ``pg_restore``, ``git worktree``.
- ``run_allowed_to_fail`` — returns the ``CompletedProcess`` when the return
    code is in ``expected_codes``; raises ``CommandFailedError`` otherwise.
    Use for probes and idempotent cleanup where the caller inspects the result.
- ``spawn`` — start a background process.  The caller owns the process
    lifetime (``.terminate()`` / ``.wait()``).
"""

import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL, PIPE, STDOUT, CompletedProcess, Popen, SubprocessError, TimeoutExpired
from typing import IO, cast

from teatree.utils.git_pins import GitPins

# The command could not be REACHED, as opposed to having run and failed: absent from
# ``PATH`` (``FileNotFoundError``), not executable (``PermissionError``), or stalled past
# its bound (``TimeoutExpired``). A caller whose read is best-effort catches this to
# degrade; one whose read is load-bearing lets it raise.
SUBPROCESS_UNREACHABLE: tuple[type[BaseException], ...] = (OSError, SubprocessError)

# A long-lived ``check=False`` caller (uvicorn / runserver via service_launch)
# emits stderr for the whole process lifetime; retaining every line leaks memory
# without bound. Only the tail is ever read back — ``CommandFailedError`` shows
# ``_last_lines`` n=20 — so a tail this size keeps the diagnostic while capping
# retention. ``run_streamed`` keeps only the last this-many stderr lines.
STREAMED_STDERR_RETAINED_LINES = 200
type ProcessStream = int | IO[bytes] | IO[str] | None

__all__ = [
    "DEVNULL",
    "PIPE",
    "STDOUT",
    "STREAMED_STDERR_RETAINED_LINES",
    "SUBPROCESS_UNREACHABLE",
    "BytePipe",
    "CommandFailedError",
    "CompletedProcess",
    "Popen",
    "TimeoutExpired",
    "redact_secrets",
    "run_allowed_to_fail",
    "run_bounded_group",
    "run_checked",
    "run_streamed",
    "spawn",
    "spawn_byte_pipe",
    "spawn_session_leader",
]


# `x-` header names are bounded to 64 characters so the scan stays linear; a longer name is not required to redact.
_SECRET_HEADER_RE = re.compile(r"(?i)(authorization|private-token|x-[\w-]{0,64}(?:token|key))\s*:\s*\S.*")
# The lookbehinds anchor the start of a hyphen-joined name run; without them every hyphen restarts a quadratic rescan.
_SECRET_QUERY_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?<![a-z0-9][_-])((?:[a-z0-9]+[_-])*(?:token|secret|key|password))=[^&\s]+"
)
# An env-var ASSIGNMENT whose name ends in a credential class — a `-e NAME=value`
# forward, an env prefix on a command. Keyed on the suffix rather than a list of
# variable names, which is a list someone forgets to extend: matching only `TOKEN`
# published `T3_SECRET_KEY` and an `ANTHROPIC_API_KEY` into transcripts.
_SECRET_ENV_ASSIGN_RE = re.compile(r"(?:\A|(?<=[=\s]))([A-Z][A-Z0-9_]*(?:KEY|SECRET|PASSWORD|TOKEN))=(\S+)")
# A value after `Bearer` is a credential only from 8 token characters on, so "the bearer of this note" stays readable.
_SECRET_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/-]{8,}=*")
# A URL userinfo's password, or a lone userinfo only when token-like (12+ characters, or a digit, underscore or hyphen).
_SECRET_URL_USERINFO_RE = re.compile(
    r"(?<=://)(?:([^\s/@:?#]*:)[^\s/@?#]+|(?=[^\s/@:?#]*[\d_-]|[^\s/@:?#]{12})[^\s/@:?#]+)(?=@)"
)
# A quoted credential-named key (JSON, or a Python dict repr) and its quoted value; both quotes must match.
_SECRET_QUOTED_PAIR_RE = re.compile(
    r"""(?i)((["'])(?:[a-z0-9]+[_-])*(?:token|secret|key|password)\2\s*:\s*)\2(?:(?!\2)[^\\]|\\.)*\2"""
)


def redact_secrets(arg: str) -> str:
    """Strip credential values from a single command-line argument.

    Public (not module-private) because callers OUTSIDE this module's own
    :class:`CommandFailedError` formatting need the same discipline — e.g. a Lane-B
    tool wrapping a raw stdlib exception (``subprocess.TimeoutExpired``) whose
    default ``str()`` echoes the unredacted command verbatim.

    The variable NAME survives every substitution: it is what makes a failure
    diagnosable, and only the value is a secret.
    """
    redacted = _SECRET_HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", arg)
    redacted = _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
    redacted = _SECRET_QUOTED_PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>{m.group(2)}", redacted)
    redacted = _SECRET_BEARER_RE.sub(lambda m: f"{m.group(1)}<redacted>", redacted)
    redacted = _SECRET_URL_USERINFO_RE.sub(lambda m: f"{m.group(1) or ''}<redacted>", redacted)
    return _SECRET_ENV_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)


class CommandFailedError(RuntimeError):
    """Raised when a subprocess exits with an unexpected return code."""

    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        self.cmd: list[str] = list(cmd)
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        super().__init__(self._format())

    def _format(self) -> str:
        cmd_str = " ".join(redact_secrets(arg) for arg in self.cmd)
        tail = _last_lines(self.stderr or self.stdout, n=20)
        if tail:
            return f"command failed (rc={self.returncode}): {cmd_str}\n{tail}"
        return f"command failed (rc={self.returncode}): {cmd_str}"


def _last_lines(text: str, *, n: int) -> str:
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[-n:])


def _git_probe_timeout(cmd: Sequence[str], timeout: float | None, started: float) -> float:
    if timeout is None:
        return 5.0
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutExpired(cmd, timeout)
    return min(remaining, 5.0)


def _trusted_hooks_path(
    cmd: Sequence[str], common_dir: str, *, env: dict[str, str] | None, cwd: str | Path | None, timeout: float
) -> str:
    probe = subprocess.run(
        GitPins.hooks_path_probe(cmd),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if probe.returncode not in {0, 1}:
        raise CommandFailedError(cmd, probe.returncode, probe.stdout, probe.stderr)
    default = str(Path(common_dir) / "hooks")
    if probe.returncode == 1:
        return default
    scope, separator, configured = probe.stdout.rstrip("\n").partition("\t")
    if not separator or not configured or "\n" in configured:
        raise CommandFailedError(cmd, 1, probe.stdout, "invalid core.hooksPath probe result")
    if scope in {"system", "global"}:
        return configured
    if Path(configured).is_absolute() and Path(configured).resolve() == Path(default).resolve():
        return default
    raise CommandFailedError(cmd, 1, "", f"untrusted {scope} core.hooksPath on control-plane git write")


def _pinned_git_argv(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
) -> tuple[list[str], float | None]:
    if not GitPins.needs_hooks(cmd):
        return GitPins.argv(cmd), timeout
    started = time.monotonic()
    probe = subprocess.run(
        GitPins.common_dir_probe(cmd),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=_git_probe_timeout(cmd, timeout, started),
        check=False,
    )
    common_dir = probe.stdout.removesuffix("\n")
    if probe.returncode != 0 or not common_dir or "\n" in common_dir or not Path(common_dir).is_absolute():
        raise CommandFailedError(cmd, probe.returncode or 1, probe.stdout, probe.stderr)
    hooks_path = _trusted_hooks_path(
        cmd,
        common_dir,
        env=env,
        cwd=cwd,
        timeout=_git_probe_timeout(cmd, timeout, started),
    )
    remaining = None
    if timeout is not None:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutExpired(cmd, timeout)
    return GitPins.argv(cmd, hooks_path=hooks_path), remaining


def run_checked(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    stdin_text: str | None = None,
    timeout: float | None = None,
) -> CompletedProcess[str]:
    """Run a command and raise ``CommandFailedError`` on non-zero exit.

    Always captures stdout/stderr as text.  Callers never silently swallow
    failures — if non-zero is expected, use :func:`run_allowed_to_fail`.
    """
    argv, remaining_timeout = _pinned_git_argv(cmd, env=env, cwd=cwd, timeout=timeout)
    result = subprocess.run(
        argv,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=remaining_timeout,
        check=False,
    )
    if result.returncode != 0:
        raise CommandFailedError(cmd, result.returncode, result.stdout, result.stderr)
    return CompletedProcess(list(cmd), result.returncode, result.stdout, result.stderr)


def _kill_process_group(process: "Popen[str]") -> None:
    """SIGKILL the whole session *process* leads, falling back to the child alone.

    ``os.getpgid`` raises once the child is reaped, and a container without the
    privilege to signal the group raises ``PermissionError``; neither may leave the
    caller with an un-killed child, so both degrade to the direct kill.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()


def run_bounded_group(
    cmd: Sequence[str],
    *,
    timeout: float,
    expected_codes: Iterable[int] | None = (0,),
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> CompletedProcess[str]:
    """Run *cmd* under a deadline that terminates the whole process GROUP.

    :func:`run_checked`'s ``timeout`` is not a bound for a command that forks.
    ``subprocess.run`` kills only the DIRECT child and then waits on the captured
    pipes, which a surviving grandchild still holds open — so the wait never ends and
    the grandchild is orphaned rather than reaped. ``pass show`` is exactly that shape
    (a bash script that execs ``gpg``), which is how a 20s credential deadline became a
    ten-hour hang with 322 orphaned ``pass``/``gpg`` pairs behind it.

    ``start_new_session`` makes the child a session leader, so the deadline can
    ``killpg`` every descendant at once. Use this for any command that shells out to a
    daemon-backed helper; :func:`run_checked` remains right for a simple leaf process.

    *expected_codes* matches :func:`run_allowed_to_fail`: ``None`` returns every
    completed exit code, while an unexpected code raises :class:`CommandFailedError`.

    Past the deadline the wait is ``wait()``, never a second ``communicate()``: a
    descendant that ``setsid``-ed out of the killed group still holds the captured pipes,
    so draining to EOF would restore the very unbounded wait this exists to remove.
    ``subprocess.run`` reserves that drain for its ``_mswindows`` branch for the same
    reason.
    """
    try:
        argv, remaining_timeout = _pinned_git_argv(cmd, env=env, timeout=timeout)
    except CommandFailedError as error:
        if expected_codes is None:
            return CompletedProcess(list(cmd), error.returncode, error.stdout, error.stderr)
        raise
    with subprocess.Popen(
        argv,
        env=env,
        stdin=PIPE if stdin_text is not None else DEVNULL,
        stdout=PIPE,
        stderr=PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(input=stdin_text, timeout=remaining_timeout)
        except TimeoutExpired:
            _kill_process_group(process)
            process.wait()
            raise
    if expected_codes is not None and process.returncode not in expected_codes:
        raise CommandFailedError(cmd, process.returncode, stdout, stderr)
    return CompletedProcess(list(cmd), process.returncode, stdout, stderr)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_allowed_to_fail(  # noqa: PLR0913 — each keyword-only param is one documented subprocess control (expected_codes/env/cwd/stdin_text/timeout), mirroring run_checked.
    cmd: Sequence[str],
    *,
    expected_codes: Iterable[int] | None = (0,),
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    stdin_text: str | None = None,
    timeout: float | None = None,
) -> CompletedProcess[str]:
    """Run a command and return the result if the exit code is expected.

    *expected_codes* controls what counts as success.  Pass a specific set
    (e.g. ``(0, 1)`` for probes where 1 means "nothing to do") or ``None``
    to accept any exit code.  Unexpected codes raise :class:`CommandFailedError`.

    *stdin_text* mirrors :func:`run_checked`: when given it is fed to the child's
    stdin (piping a dump/SQL/credential in rather than materialising a temp file).
    """
    try:
        argv, remaining_timeout = _pinned_git_argv(cmd, env=env, cwd=cwd, timeout=timeout)
    except CommandFailedError as error:
        if expected_codes is None:
            return CompletedProcess(list(cmd), error.returncode, error.stdout, error.stderr)
        raise
    result = subprocess.run(
        argv,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=remaining_timeout,
        check=False,
    )
    if expected_codes is not None and result.returncode not in expected_codes:
        raise CommandFailedError(cmd, result.returncode, result.stdout, result.stderr)
    return CompletedProcess(list(cmd), result.returncode, result.stdout, result.stderr)


def run_streamed(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    check: bool = True,
) -> int:
    """Run a command, inheriting stdin/stdout, teeing stderr live + captured.

    Use for interactive commands where the user needs live output (Django
    management commands, ``uvicorn``, ``tail -f``).  Returns the exit code.
    stdout stays inherited so live output and interactive prompts work; stderr
    is teed — each chunk is forwarded to the parent's ``stderr`` *and*
    captured — so that when ``check`` is True a non-zero exit raises
    :class:`CommandFailedError` carrying the subcommand's stderr. Without the
    capture, a wrapped failure surfaces as a bare ``command failed (rc=1)``
    with no clue *why* (the #1750 ``--thread-ts`` breakage was invisible for
    exactly this reason).

    The captured stderr is bounded to the last
    :data:`STREAMED_STDERR_RETAINED_LINES` lines via a fixed-size deque. A
    long-lived ``check=False`` server (uvicorn / runserver) would otherwise
    accumulate stderr for its whole lifetime; only the tail is ever read back
    (the error shows ``_last_lines`` n=20), so capping retention keeps the
    diagnostic without the unbounded growth.
    """
    captured: deque[str] = deque(maxlen=STREAMED_STDERR_RETAINED_LINES)
    try:
        argv, _ = _pinned_git_argv(cmd, env=env, cwd=cwd)
    except CommandFailedError as error:
        if check:
            raise
        sys.stderr.write(error.stderr)
        return error.returncode
    with Popen(
        argv,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        stderr=PIPE,
        text=True,
    ) as proc:
        for line in cast("IO[str]", proc.stderr):
            sys.stderr.write(line)
            captured.append(line)
        sys.stderr.flush()
        returncode = proc.wait()
    if check and returncode != 0:
        raise CommandFailedError(cmd, returncode, "", "".join(captured))
    return returncode


def spawn(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    stdout: int | IO[bytes] | IO[str] | None = None,
    stderr: int | IO[bytes] | IO[str] | None = None,
) -> Popen[str]:
    """Spawn a background process.  Caller owns the lifetime.

    Pass ``stdout``/``stderr`` explicitly (``DEVNULL``, ``PIPE``, ``STDOUT``,
    or a file handle) — when both are ``None`` the streams inherit the
    parent's.
    """
    argv, _ = _pinned_git_argv(cmd, env=env, cwd=cwd)
    return subprocess.Popen(
        argv,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )


@dataclass(frozen=True, slots=True)
class BytePipe:
    """A spawned child whose binary ``stdin``/``stdout`` pipes are both known present.

    The narrowing exists once here so no caller needs a defensive ``is None`` branch for
    two pipes :func:`spawn_byte_pipe` always opens.
    """

    process: Popen[bytes]
    stdin: IO[bytes]
    stdout: IO[bytes]


def spawn_byte_pipe(cmd: Sequence[str]) -> BytePipe:
    """Spawn a background process with BINARY stdin/stdout pipes for the caller to pump.

    Distinct from :func:`spawn`, which is text-mode: a caller relaying an OPAQUE byte
    stream (an HTTP body, a compressed payload) must not have it decoded and re-encoded.
    ``stderr`` is discarded — nothing reads it, and an unread PIPE can fill and block the child.
    """
    argv, _ = _pinned_git_argv(cmd)
    process = subprocess.Popen(argv, stdin=PIPE, stdout=PIPE, stderr=DEVNULL)
    return BytePipe(process=process, stdin=cast("IO[bytes]", process.stdin), stdout=cast("IO[bytes]", process.stdout))


def spawn_session_leader(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    stdio: tuple[ProcessStream, ProcessStream, ProcessStream] = (DEVNULL, None, None),
) -> Popen[str]:
    """Spawn a background process that LEADS its own process group.

    ``start_new_session=True`` makes the child a session/group leader, so the
    caller can ``os.killpg(os.getpgid(proc.pid), …)`` the WHOLE group (the child
    plus any grandchildren it spawns) on a deadline — an OS-level kill boundary a
    plain :func:`spawn` cannot give. ``stdio[0]`` defaults to ``DEVNULL`` so a detached
    process cannot block on the parent's input. ``env``
    replaces the child's environment when given (pass ``{**os.environ, …}`` to extend).
    """
    argv, _ = _pinned_git_argv(cmd, env=env, cwd=cwd)
    return subprocess.Popen(
        argv,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        stdin=stdio[0],
        stdout=stdio[1],
        stderr=stdio[2],
        start_new_session=True,
        text=True,
    )
