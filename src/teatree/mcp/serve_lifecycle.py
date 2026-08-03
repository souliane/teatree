"""Orphan hygiene for ``t3 mcp serve`` — reap-on-start + a parent-death watchdog.

A stdio MCP server is 1:1 with the client process that spawned it: once that
parent is gone, the server's stdin pipe can never carry another request, so the
process is pure leaked RAM + an idle Django stack. The normal exit path is
stdin EOF, but a parent that is SIGKILLed while a grandchild still holds the
pipe's write end — or any fd-inheritance race — leaves the server alive and
reparented to PID 1. Those accumulated on a real host (8 concurrent servers).

Two complementary levers, both wired into ``t3 mcp serve``:

*   :func:`reap_orphaned_servers` — at startup, SIGTERM every OTHER
    ``t3 mcp serve`` process whose parent is PID 1. Reparenting to init is the
    structural proof of disconnection (a live client's server keeps its real
    parent), so servers serving live sessions are never touched.
*   :func:`start_parent_death_watch` — a daemon thread that polls
    ``os.getppid()``; the moment this process is reparented to PID 1 the
    client is gone, and the server hard-exits instead of waiting for an EOF
    that may never arrive.

Both levers read PID 1 as "reparented to init", which is a HOST fact. In a
container PID 1 is the container's own entrypoint, so a server started by
``docker compose run --entrypoint t3 … mcp serve`` has ``getppid() == 1`` from
its first instant: the watchdog fired immediately and the server exited 0
without answering a single request, and the reaper would SIGTERM every sibling
server in the same container. :func:`orphan_detection_applies` therefore makes
both inert there — the container runtime owns that lifecycle, tearing the exec
down when the client disconnects.
"""

import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from teatree.docker.workflow import is_running_in_container
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

logger = logging.getLogger(__name__)

_INIT_PID = 1
_DEFAULT_POLL_SECONDS = 5.0
_PS_FIELDS = 3


def orphan_detection_applies(*, containerized: bool | None = None) -> bool:
    """Whether "reparented to PID 1" proves the client is gone.

    Only on a host. In a container PID 1 is the entrypoint, so the proof is
    vacuously true for every process in there — including a healthy server that
    has not yet read its first byte.
    """
    return not (is_running_in_container() if containerized is None else containerized)


class ProcessRecord(NamedTuple):
    """One row of the process table: pid, parent pid, and the full command line."""

    pid: int
    ppid: int
    command: str


def process_snapshot() -> list[ProcessRecord]:
    """The current process table via ``ps`` (portable across macOS and Linux).

    Fails open to an empty snapshot on any ``ps`` failure — orphan reaping is
    hygiene, never worth failing server startup over.
    """
    try:
        out = run_allowed_to_fail(["ps", "-axo", "pid=,ppid=,command="]).stdout
    except (OSError, CommandFailedError):
        return []
    records = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < _PS_FIELDS or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        records.append(ProcessRecord(pid=int(parts[0]), ppid=int(parts[1]), command=parts[2]))
    return records


def is_serve_command(command: str) -> bool:
    """Whether *command* is a ``t3 mcp serve`` invocation.

    Matches the ``t3`` token (bare or a path ending in ``/t3``) immediately
    followed by ``mcp serve``, where ``t3`` is either argv[0] or preceded by a
    python interpreter (the shebang-rewritten form ``…/python …/bin/t3 mcp
    serve``). The interpreter constraint keeps an unrelated process whose
    ARGUMENTS merely mention ``t3 mcp serve`` (a grep, an editor) out of the
    match — reaping must never guess.
    """
    words = command.split()
    for index in range(len(words) - 2):
        word = words[index]
        if word != "t3" and not word.endswith("/t3"):
            continue
        if words[index + 1] != "mcp" or words[index + 2] != "serve":
            continue
        if index == 0:
            return True
        if Path(words[index - 1]).name.startswith("python"):
            return True
    return False


def orphaned_serve_pids(
    records: Iterable[ProcessRecord],
    *,
    self_pid: int,
    matcher: Callable[[str], bool] = is_serve_command,
) -> list[int]:
    """PIDs of ``t3 mcp serve`` processes reparented to PID 1 — never our own.

    PPID 1 is the structural disconnection proof: a stdio server whose parent
    died can never receive another request. A server with any live parent (a
    running session, a warm spare) is out of scope by construction.
    """
    return [
        record.pid
        for record in records
        if record.ppid == _INIT_PID and record.pid != self_pid and matcher(record.command)
    ]


def reap_orphaned_servers(*, matcher: Callable[[str], bool] = is_serve_command) -> list[int]:
    """SIGTERM every orphaned ``t3 mcp serve`` process; return the pids reaped.

    Best-effort per pid: a process that exited meanwhile or one we may not
    signal is skipped silently. Called once at ``t3 mcp serve`` startup, so
    every new server spawn is also the garbage collection of its predecessors.
    Inert in a container, where PID 1 proves nothing (see
    :func:`orphan_detection_applies`) and this would SIGTERM every sibling.
    """
    if not orphan_detection_applies():
        return []
    reaped = []
    for pid in orphaned_serve_pids(process_snapshot(), self_pid=os.getpid(), matcher=matcher):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        reaped.append(pid)
    if reaped:
        logger.info("reaped %d orphaned `t3 mcp serve` process(es): %s", len(reaped), reaped)
    return reaped


def watch_until_orphaned(
    *,
    get_ppid: Callable[[], int] = os.getppid,
    on_orphaned: Callable[[], None],
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
) -> None:
    """Block until this process is reparented to PID 1, then react.

    Reparenting to init is the same structural disconnection proof the
    startup reaper uses — it happens only when the launching client is dead.
    Deliberately NOT "any ppid change": PID 1 keeps the watchdog inert on
    exotic subreaper topologies, where a conservative miss (the startup reaper
    of the next spawn still catches it) beats a false-positive self-kill. The
    loop never returns on a healthy parent; it runs on a daemon thread.
    """
    while get_ppid() != _INIT_PID:
        time.sleep(poll_seconds)
    on_orphaned()


def _hard_exit() -> None:
    """Exit the orphaned server immediately (``os._exit`` — no atexit, no flush).

    The MCPServer anyio loop is blocked on a stdin that will never close; a
    cooperative shutdown has nothing to cooperate with, and an orphan has no
    in-flight client work to preserve. Mirrors the ``loops_tick`` deadline exit.
    """
    os._exit(0)


def start_parent_death_watch(*, poll_seconds: float = _DEFAULT_POLL_SECONDS) -> threading.Thread | None:
    """Start the daemon watchdog that exits this process when its parent dies.

    Covers the case stdin EOF misses: the parent is gone but a leaked fd keeps
    the pipe open, so the blocking stdio read never returns. Daemon, so it can
    never keep the process alive itself.

    Returns ``None`` in a container, where the watch would fire on the first poll
    against PID 1 and hard-exit a server that has answered nothing (see
    :func:`orphan_detection_applies`); the container runtime ends the process when
    the client's exec session goes away.
    """
    if not orphan_detection_applies():
        return None
    thread = threading.Thread(
        target=watch_until_orphaned,
        kwargs={"on_orphaned": _hard_exit, "poll_seconds": poll_seconds},
        name="mcp-serve-parent-death-watch",
        daemon=True,
    )
    thread.start()
    return thread
