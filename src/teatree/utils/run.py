"""Typed subprocess wrappers.

Every ``subprocess.run`` / ``subprocess.Popen`` call in ``src/teatree`` MUST go
through these wrappers.  Raw subprocess usage is banned by the
``check_subprocess_ban`` prek hook (see ``scripts/hooks/check_subprocess_ban.py``).

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

import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from subprocess import DEVNULL, PIPE, STDOUT, CompletedProcess, Popen, TimeoutExpired
from typing import IO

__all__ = [
    "DEVNULL",
    "PIPE",
    "STDOUT",
    "CommandFailedError",
    "CompletedProcess",
    "Popen",
    "TimeoutExpired",
    "run_allowed_to_fail",
    "run_checked",
    "run_streamed",
    "spawn",
]


_SECRET_HEADER_RE = re.compile(r"(?i)(authorization|x-[\w-]*token|x-[\w-]*key)\s*:\s*\S.*")
_SECRET_QUERY_RE = re.compile(r"(?i)\b(token|access_token|api_key|password|secret)=[^&\s]+")


def _redact_secrets(arg: str) -> str:
    """Strip credential values from a single command-line argument."""
    redacted = _SECRET_HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", arg)
    return _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)


class CommandFailedError(RuntimeError):
    """Raised when a subprocess exits with an unexpected return code."""

    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        self.cmd: list[str] = list(cmd)
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        super().__init__(self._format())

    def _format(self) -> str:
        cmd_str = " ".join(_redact_secrets(arg) for arg in self.cmd)
        tail = _last_lines(self.stderr or self.stdout, n=20)
        if tail:
            return f"command failed (rc={self.returncode}): {cmd_str}\n{tail}"
        return f"command failed (rc={self.returncode}): {cmd_str}"


def _last_lines(text: str, *, n: int) -> str:
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[-n:])


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
    result = subprocess.run(
        list(cmd),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise CommandFailedError(cmd, result.returncode, result.stdout, result.stderr)
    return result


def run_allowed_to_fail(
    cmd: Sequence[str],
    *,
    expected_codes: Iterable[int] | None = (0,),
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
) -> CompletedProcess[str]:
    """Run a command and return the result if the exit code is expected.

    *expected_codes* controls what counts as success.  Pass a specific set
    (e.g. ``(0, 1)`` for probes where 1 means "nothing to do") or ``None``
    to accept any exit code.  Unexpected codes raise :class:`CommandFailedError`.
    """
    result = subprocess.run(
        list(cmd),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if expected_codes is not None and result.returncode not in expected_codes:
        raise CommandFailedError(cmd, result.returncode, result.stdout, result.stderr)
    return result


def run_streamed(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    check: bool = True,
) -> int:
    """Run a command with stdin/stdout/stderr inherited from the parent.

    Use for interactive commands where the user needs live output (Django
    management commands, ``uvicorn``, ``tail -f``).  Returns the exit code.
    When ``check`` is True (default), non-zero exits raise
    :class:`CommandFailedError` with no captured output (streams went to the
    terminal, not to buffers).
    """
    proc = subprocess.run(
        list(cmd),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        check=False,
    )
    if check and proc.returncode != 0:
        raise CommandFailedError(cmd, proc.returncode, "", "")
    return proc.returncode


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
    return subprocess.Popen(
        list(cmd),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
