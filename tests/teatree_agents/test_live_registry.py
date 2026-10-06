"""A command whose delivery the owner loop never confirmed stays ``unknown_delivery`` and is never replayed."""

import asyncio
import threading
from unittest.mock import patch

from teatree.agents import live_registry
from teatree.agents.live_control import ControlOutcome, ControlReceipt, LiveTask
from teatree.agents.live_registry import LiveSessionRegistry

_TASK = LiveTask(pk=7, ticket=3, phase="coding", harness="claude_sdk", model="m")


class _Capture:
    tool_calls = 0
    context_tokens = None
    last_event_at = None
    open_tool = None
    result_message = None


class _SilentSession:
    """Takes a steer and never says whether the running turn accepted it."""

    def __init__(self) -> None:
        self.steered: list[str] = []

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del text, wait
        self.steered.append(input_id)
        await asyncio.Event().wait()

    async def interrupt(self) -> None:
        return None


class _OwnerLoop(threading.Thread):
    """The session's own event loop, holding it live in the registry until ``end``."""

    def __init__(self, registry: LiveSessionRegistry, session: _SilentSession) -> None:
        super().__init__(daemon=True)
        self._registry = registry
        self._session = session
        self._loop: asyncio.AbstractEventLoop | None = None
        self._finish: asyncio.Event | None = None
        self.attached = threading.Event()

    def run(self) -> None:
        asyncio.run(self._own())

    async def _own(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._finish = asyncio.Event()
        async with self._registry.attach(_TASK, _Capture()) as controller:
            controller.bind(self._session)
            self.attached.set()
            await self._finish.wait()

    def end(self) -> None:
        assert self._loop is not None
        assert self._finish is not None
        self._loop.call_soon_threadsafe(self._finish.set)
        self.join(timeout=10)


def _steer(registry: LiveSessionRegistry) -> ControlReceipt:
    return asyncio.run(registry.steer(_TASK.pk, "use the attached page", command_id="c-1", wait=0.05))


def test_an_unconfirmed_command_stays_unknown_for_every_retry_and_is_delivered_once() -> None:
    registry = LiveSessionRegistry()
    session = _SilentSession()
    owner = _OwnerLoop(registry, session)
    owner.start()
    assert owner.attached.wait(timeout=10)

    with patch.object(live_registry, "_OWNER_LOOP_GRACE_SECONDS", 0.05):
        first = _steer(registry)
        retried_while_live = _steer(registry)
        owner.end()
        retried_after_close = _steer(registry)

    outcomes = [first.outcome, retried_while_live.outcome, retried_after_close.outcome]
    assert outcomes == [ControlOutcome.UNKNOWN_DELIVERY] * 3
    assert retried_after_close.code is None
    assert session.steered == ["c-1"]
