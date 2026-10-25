"""The worker's addressable map of live sessions; only a session's own event loop ever touches it."""

import asyncio
import os
import threading
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass

from teatree.agents.live_control import (
    ControlOutcome,
    ControlReceipt,
    LiveFence,
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


@dataclass(frozen=True, slots=True)
class _OperatorCommand:
    text: str
    command_id: str
    wait: float
    kind: str
    fence: LiveFence


class LiveSessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[int, _LiveEntry] = {}
        self._closed: OrderedDict[int, LiveSessionController] = OrderedDict()

    @asynccontextmanager
    async def attach(
        self, task: LiveTask, capture: StreamFacts, *, claim_live: Callable[[], Awaitable[bool]] | None = None
    ) -> AsyncGenerator[LiveSessionController]:
        """Publish *task* for the life of the block; its controller closes and settles on exit."""
        entry = _LiveEntry(LiveSessionController(task, capture, claim_live), asyncio.get_running_loop())
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

    async def steer(self, task_pk: int, text: str, *, command_id: str, wait: float, fence: LiveFence) -> ControlReceipt:
        """Hand the command to the session's owning loop and await its receipt from any other loop."""
        return await self._operator_command(task_pk, _OperatorCommand(text, command_id, wait, "steer", fence))

    async def follow_up(
        self, task_pk: int, text: str, *, command_id: str, wait: float, fence: LiveFence
    ) -> ControlReceipt:
        return await self._operator_command(task_pk, _OperatorCommand(text, command_id, wait, "follow_up", fence))

    async def pause(self, task_pk: int, text: str, *, command_id: str, wait: float, fence: LiveFence) -> ControlReceipt:
        return await self._operator_command(task_pk, _OperatorCommand(text, command_id, wait, "pause", fence))

    async def _operator_command(self, task_pk: int, request: _OperatorCommand) -> ControlReceipt:
        with self._lock:
            entry = self._live.get(task_pk)
            tombstone = self._closed.get(task_pk)
            target = entry.controller if entry is not None else tombstone
            if target is not None and target.task.fence != request.fence:
                return ControlReceipt.rejected(request.command_id, task_pk, RejectCode.STALE_COMMAND)
        if entry is None:
            if tombstone is None:
                return ControlReceipt.rejected(request.command_id, task_pk, RejectCode.UNKNOWN_SESSION)
            known = tombstone.known_receipt(request.command_id, request.text, request.kind)
            return known or ControlReceipt.rejected(request.command_id, task_pk, RejectCode.SESSION_CLOSED)
        if request.kind == "pause":
            command = entry.controller.pause(request.text, command_id=request.command_id)
        else:
            method = entry.controller.follow_up if request.kind == "follow_up" else entry.controller.steer
            command = method(request.text, command_id=request.command_id, wait=request.wait)
        return await self._on_owner_loop(entry, command, command_id=request.command_id, wait=request.wait)

    async def push(
        self, task_pk: int, framed: str, *, command_id: str, wait: float, fence: LiveFence
    ) -> ControlReceipt:
        """A peer message into the session's current turn; a closed session keeps no peer receipt."""
        with self._lock:
            entry = self._live.get(task_pk)
            closed = task_pk in self._closed
            target = entry.controller if entry is not None else self._closed.get(task_pk)
            if target is not None and target.task.fence != fence:
                return ControlReceipt.rejected(command_id, task_pk, RejectCode.STALE_COMMAND)
        if entry is None:
            code = RejectCode.SESSION_CLOSED if closed else RejectCode.UNKNOWN_SESSION
            return ControlReceipt.rejected(command_id, task_pk, code)
        command = entry.controller.push(framed, command_id=command_id, wait=wait)
        return await self._on_owner_loop(entry, command, command_id=command_id, wait=wait)

    @staticmethod
    async def _on_owner_loop(
        entry: _LiveEntry, command: Coroutine[object, object, ControlReceipt], *, command_id: str, wait: float
    ) -> ControlReceipt:
        task_pk = entry.controller.task.pk
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
