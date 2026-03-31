"""In-memory process registry for tracking spawned subprocesses.

Tracks Popen handles so they can be cleaned up on shutdown or
via a dashboard action. Processes die with the server, so
persistence across restarts is not needed.
"""

import atexit
import logging
import subprocess  # noqa: S404
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessEntry:
    process: subprocess.Popen[str]
    description: str
    started_at: float = field(default_factory=time.monotonic)

    @property
    def alive(self) -> bool:
        return self.process.poll() is None


_registry: dict[int, ProcessEntry] = {}


def register(process: subprocess.Popen[str], description: str) -> int:
    pid = process.pid
    _registry[pid] = ProcessEntry(process=process, description=description)
    logger.debug("Registered process %d: %s", pid, description)
    return pid


def unregister(pid: int) -> None:
    _registry.pop(pid, None)


def cleanup_exited() -> int:
    exited = [pid for pid, entry in _registry.items() if not entry.alive]
    for pid in exited:
        _registry.pop(pid, None)
    return len(exited)


def terminate_all() -> int:
    count = 0
    for pid, entry in list(_registry.items()):
        if entry.alive:
            logger.info("Terminating process %d: %s", pid, entry.description)
            entry.process.terminate()
            count += 1
    _registry.clear()
    return count


def list_processes() -> list[dict[str, object]]:
    cleanup_exited()
    return [
        {
            "pid": pid,
            "description": entry.description,
            "alive": entry.alive,
            "uptime_seconds": int(time.monotonic() - entry.started_at),
        }
        for pid, entry in _registry.items()
    ]


atexit.register(terminate_all)
