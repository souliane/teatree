"""Runs inside the read-only executor as ``python3 -I -S -``: ends every process but PID 1's session and its own.

Nothing a process can change about itself decides it: no process joins PID 1's session after the fact, and
no process inside the container gets a parent outside it, which only ``docker exec`` hands out.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import NamedTuple, TypedDict

ATTEMPTS = 20
PAUSE_SECONDS = 0.05


class SweepRequest(TypedDict):
    home_root: str


class SweepAnswer(TypedDict):
    survivors: list[int]


class SweepError(RuntimeError):
    pass


class _Process(NamedTuple):
    pid: int
    state: str
    ppid: int
    sid: int
    entry: Path


def _process(entry: Path) -> _Process | None:
    try:
        head, _, tail = (entry / "stat").read_text(encoding="utf-8").rpartition(")")
        fields = tail.split()
        return _Process(int(head.split(" (", 1)[0]), fields[0], int(fields[1]), int(fields[3]), entry)
    except (OSError, IndexError, ValueError):
        return None


def _codex_home(entry: Path) -> str | None:
    try:
        environ = (entry / "environ").read_bytes()
    except OSError:
        return None
    for item in environ.split(b"\0"):
        name, _, value = item.partition(b"=")
        if name == b"CODEX_HOME":
            return value.decode(errors="replace")
    return ""


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _spared_exec_root(process: _Process, home_root: str) -> bool:
    """A ``docker exec`` program of the worker's: its parent is outside the container, and it runs no Codex."""
    home = _codex_home(process.entry)
    return home is not None and not _under(home, home_root)


def strays(home_root: str, proc: Path) -> list[int]:
    """Every live process outside PID 1's session, but this sweep and the worker's own exec programs."""
    init = _process(proc / "1")
    if init is None:
        msg = f"PID 1 is unreadable under {proc}, so its session cannot be spared"
        raise SweepError(msg)
    table = {
        process.pid: process
        for entry in proc.iterdir()
        if entry.name.isdigit() and (process := _process(entry)) is not None
    }
    return sorted(
        process.pid
        for process in table.values()
        if process.state not in {"Z", "X"}
        and process.pid != os.getpid()
        and process.sid != init.sid
        and (process.ppid in table or not _spared_exec_root(process, home_root))
    )


def sweep(request: SweepRequest, proc: Path = Path("/proc")) -> SweepAnswer:
    for _ in range(ATTEMPTS):
        victims = strays(request["home_root"], proc)
        if not victims:
            break
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        time.sleep(PAUSE_SECONDS)
    return SweepAnswer(survivors=strays(request["home_root"], proc))


def main() -> None:
    sys.stdout.write(json.dumps(sweep(json.loads(sys.argv[1]))))


if __name__ == "__main__":
    main()
