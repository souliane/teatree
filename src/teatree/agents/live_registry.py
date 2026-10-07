"""The worker's addressable map of live sessions; only a session's own event loop ever touches it."""

import asyncio
import os
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from teatree.agents.live_control import (
    ControlOutcome,
    ControlReceipt,
    LiveSessionController,
    LiveTask,
    RejectCode,
    SessionFacts,
    StreamFacts,
)

TOMBSTONES = 256
_OWNER_LOOP_GRACE_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class _LiveEntry:
    controller: LiveSessionController
    loop: asyncio.AbstractEventLoop


class LiveSessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[int, _LiveEntry] = {}
        self._closed: OrderedDict[int, LiveSessionController] = OrderedDict()

    @asynccontextmanager
    async def attach(self, task: LiveTask, capture: StreamFacts) -> AsyncIterator[LiveSessionController]:
        """Publish *task* for the life of the block; its controller closes and settles on exit."""
        entry = _LiveEntry(LiveSessionController(task, capture), asyncio.get_running_loop())
        with self._lock:
            self._live[task.pk] = entry
            self._closed.pop(task.pk, None)
        try:
            yield entry.controller
        finally:
            entry.controller.close()
            await entry.controller.drain()
            with self._lock:
                if self._live.get(task.pk) is entry:
                    del self._live[task.pk]
                self._closed[task.pk] = entry.controller
                while len(self._closed) > TOMBSTONES:
                    self._closed.popitem(last=False)

    def sessions(self) -> list[SessionFacts]:
        with self._lock:
            controllers = [entry.controller for entry in self._live.values()]
        return [controller.facts() for controller in controllers]

    def facts(self, task_pk: int) -> SessionFacts | None:
        with self._lock:
            entry = self._live.get(task_pk)
        return entry.controller.facts() if entry is not None else None

    async def steer(self, task_pk: int, text: str, *, command_id: str, wait: float) -> ControlReceipt:
        """Hand the command to the session's owning loop and await its receipt from any other loop."""
        with self._lock:
            entry = self._live.get(task_pk)
            tombstone = self._closed.get(task_pk)
        if entry is None:
            if tombstone is None:
                return ControlReceipt.rejected(command_id, task_pk, RejectCode.UNKNOWN_SESSION)
            known = tombstone.known_receipt(command_id, text)
            return known or ControlReceipt.rejected(command_id, task_pk, RejectCode.SESSION_CLOSED)
        command = entry.controller.steer(text, command_id=command_id, wait=wait)
        try:
            handed = asyncio.run_coroutine_threadsafe(command, entry.loop)
        except RuntimeError:
            command.close()
            return ControlReceipt.rejected(command_id, task_pk, RejectCode.SESSION_CLOSED)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(handed), wait + _OWNER_LOOP_GRACE_SECONDS)
        except TimeoutError:
            return ControlReceipt(command_id, task_pk, ControlOutcome.UNKNOWN_DELIVERY)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return ControlReceipt(command_id, task_pk, ControlOutcome.UNKNOWN_DELIVERY)


_shared_registries: dict[int, LiveSessionRegistry] = {}
_shared_lock = threading.Lock()


def shared_registry() -> LiveSessionRegistry:
    pid = os.getpid()
    with _shared_lock:
        return _shared_registries.setdefault(pid, LiveSessionRegistry())


def reset_shared_registries() -> None:
    """Forget every process-local registry between tests."""
    with _shared_lock:
        _shared_registries.clear()
