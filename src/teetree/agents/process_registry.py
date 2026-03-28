import logging
import os
import signal
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProcessEntry:
    pid: int
    description: str
    task_id: int | None = None


class ProcessRegistry:
    def __init__(self) -> None:
        self._entries: dict[int, ProcessEntry] = {}

    def register(self, pid: int, description: str, *, task_id: int | None = None) -> None:
        self._entries[pid] = ProcessEntry(pid=pid, description=description, task_id=task_id)
        logger.debug("Registered process pid=%d: %s", pid, description)

    def unregister(self, pid: int) -> None:
        self._entries.pop(pid, None)

    def cleanup_stale(self) -> list[int]:
        cleaned = []
        for pid in list(self._entries):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                self._entries.pop(pid, None)
                cleaned.append(pid)
        return cleaned

    def terminate_all(self) -> list[int]:
        terminated = []
        for pid, entry in list(self._entries.items()):
            try:
                os.kill(pid, signal.SIGTERM)
                terminated.append(pid)
                logger.info("Terminated pid=%d (%s)", pid, entry.description)
            except (ProcessLookupError, PermissionError):
                pass
            self._entries.pop(pid, None)
        return terminated

    @property
    def active(self) -> list[ProcessEntry]:
        return list(self._entries.values())


registry = ProcessRegistry()
