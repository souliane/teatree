"""Runs inside the read-only executor as ``python3 -I -S -``: ends what a Codex home started, never PID 1's session."""

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
    state: str
    ppid: int
    sid: int


def _process(entry: Path) -> _Process | None:
    try:
        fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return _Process(fields[0], int(fields[1]), int(fields[3]))


def _codex_home(entry: Path) -> str:
    try:
        environ = (entry / "environ").read_bytes()
    except OSError:
        return ""
    for item in environ.split(b"\0"):
        name, _, value = item.partition(b"=")
        if name == b"CODEX_HOME":
            return value.decode(errors="replace")
    return ""


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def codex_processes(home_root: str, proc: Path) -> list[int]:
    """Every live process with a Codex home under *home_root*, its session and its descendants."""
    init = _process(proc / "1")
    if init is None:
        msg = f"PID 1 is unreadable under {proc}, so its session cannot be spared"
        raise SweepError(msg)
    live: dict[int, _Process] = {}
    for entry in proc.iterdir():
        if entry.name.isdigit() and int(entry.name) not in {1, os.getpid()}:
            process = _process(entry)
            if process is not None and process.state not in {"Z", "X"}:
                live[int(entry.name)] = process
    marked = {pid for pid in live if _under(_codex_home(proc / str(pid)), home_root)}
    while more := {
        pid
        for pid, process in live.items()
        if pid not in marked and (process.ppid in marked or process.sid in {live[mark].sid for mark in marked})
    }:
        marked |= more
    return sorted(pid for pid in marked if live[pid].sid != init.sid)


def sweep(request: SweepRequest, proc: Path = Path("/proc")) -> SweepAnswer:
    for _ in range(ATTEMPTS):
        victims = codex_processes(request["home_root"], proc)
        if not victims:
            break
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        time.sleep(PAUSE_SECONDS)
    return SweepAnswer(survivors=codex_processes(request["home_root"], proc))


def main() -> None:
    sys.stdout.write(json.dumps(sweep(json.loads(sys.argv[1]))))


if __name__ == "__main__":
    main()
