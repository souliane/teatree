"""A live session held on its own event loop thread, the way a running task's driver holds it."""

import asyncio
import threading
from collections.abc import Callable
from typing import Self

from teatree.agents.live_control import InterruptibleSession, LiveTask, RejectCode, SteerRejectedError
from teatree.agents.live_registry import LiveSessionRegistry


class _Capture:
    tool_calls = 0
    context_tokens = None
    compactions = 0
    last_event_at = None
    open_tool = None
    result_message = None


class OwnerLoop(threading.Thread):
    """The session's own event loop, holding it live in the registry until ``end``."""

    def __init__(self, registry: LiveSessionRegistry, task: LiveTask, session: InterruptibleSession) -> None:
        super().__init__(daemon=True)
        self._registry = registry
        self._task = task
        self._session = session
        self._loop: asyncio.AbstractEventLoop | None = None
        self._finish: asyncio.Event | None = None
        self.attached = threading.Event()

    def __enter__(self) -> Self:
        self.start()
        assert self.attached.wait(timeout=10)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.is_alive():
            self.end()

    def run(self) -> None:
        asyncio.run(self._own())

    async def _own(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._finish = asyncio.Event()
        async with self._registry.attach(self._task, _Capture()) as controller:
            controller.bind(self._session)
            self.attached.set()
            await self._finish.wait()

    def call_soon(self, callback: Callable[[], None]) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(callback)

    def end(self) -> None:
        assert self._finish is not None
        self.call_soon(self._finish.set)
        self.join(timeout=10)


class BoundarySession:
    """Holds each input until the test opens the turn's next boundary; counts withdrawals."""

    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.pending = 0
        self.withdrawn = 0
        self._boundary: asyncio.Event | None = None

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del input_id
        self._boundary = self._boundary or asyncio.Event()
        self.pending += 1
        try:
            await asyncio.wait_for(self._boundary.wait(), wait)
        except TimeoutError:
            raise SteerRejectedError(RejectCode.NOT_ACCEPTED_IN_TIME) from None
        except asyncio.CancelledError:
            self.withdrawn += 1
            raise
        finally:
            self.pending -= 1
        self.delivered.append(text)

    def open_boundary(self) -> None:
        assert self._boundary is not None
        self._boundary.set()

    async def interrupt(self) -> None:
        return None
