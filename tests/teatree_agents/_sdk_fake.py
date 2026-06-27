"""Shared in-process ``claude-agent-sdk`` test double for the headless runner.

``run_headless`` drives ``ClaudeSDKClient`` (an async context manager) in
process, so tests patch the client with a stand-in that yields a canned typed
message stream. It also stubs ``TaskUsage.for_task`` to a static snapshot: the
runner samples accumulated deltas via ``asyncio.to_thread`` before the run, and
a threaded DB read under ``TestCase``'s wrapping SQLite transaction is a harness
artifact that deadlocks the connection — not production behaviour.
"""

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, Self
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, TextBlock
from claude_agent_sdk.types import RateLimitInfo, RateLimitStatus, RateLimitType

import teatree.agents.headless as headless_mod
from teatree.agents.headless import TaskUsage


def result_message(**overrides: Any) -> ResultMessage:
    """Build a typed terminal :class:`ResultMessage`.

    Accepts any :class:`ResultMessage` field as a keyword override; the rest
    take healthy-success defaults. The fields tests vary are ``session_id``,
    ``is_error``, ``num_turns``, ``total_cost_usd``, ``usage``, ``model_usage``,
    and ``result``.
    """
    defaults: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": "",
        "total_cost_usd": None,
        "usage": None,
        "result": None,
        "model_usage": None,
    }
    return ResultMessage(**{**defaults, **overrides})


def assistant_text(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-4-8[1m]")


def rate_limit_event(rate_limit_type: RateLimitType, *, status: RateLimitStatus = "rejected") -> RateLimitEvent:
    """A typed :class:`RateLimitEvent` carrying the SDK's unambiguous window.

    ``status="rejected"`` is the hard-limit-hit signal the classifier acts on.
    """
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status=status, rate_limit_type=rate_limit_type),
        uuid="rl-1",
        session_id="s1",
    )


def success_stream(result: dict[str, Any], **result_kwargs: Any) -> list[Any]:
    """A canned ``[AssistantMessage(json), ResultMessage(success)]`` stream."""
    return [assistant_text(json.dumps(result)), result_message(**result_kwargs)]


class FakeSdkClient:
    """Stand-in for ``ClaudeSDKClient`` yielding a canned typed-message stream.

    Records the ``options`` it was constructed with on the class so a test can
    assert what the runner passed (model, cwd, resume).
    """

    last_options: Any = None
    last_prompt: str = ""

    def __init__(self, messages: list[Any], *, delay: float = 0.0) -> None:
        self._messages = messages
        self._delay = delay
        self.interrupted = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        type(self).last_prompt = prompt

    async def receive_response(self) -> AsyncIterator[Any]:
        import asyncio  # noqa: PLC0415

        for message in self._messages:
            # Model the real SDK: once ``interrupt()`` lands the server stops
            # streaming, so the consumer never sees the remaining messages. The
            # fake used to drain the whole canned list regardless, so a watchdog
            # test with a long stream paid the full per-message ``delay`` even
            # though the run was interrupted on the first heartbeat tick.
            if self.interrupted:
                return
            if self._delay:
                await asyncio.sleep(self._delay)
            yield message

    async def interrupt(self) -> None:
        self.interrupted = True


@contextlib.contextmanager
def fake_sdk(
    messages: list[Any],
    *,
    delay: float = 0.0,
    task_usage: TaskUsage | None = None,
) -> Iterator[type[FakeSdkClient]]:
    """Patch ``ClaudeSDKClient`` (+ ``TaskUsage.for_task``) for a headless run."""
    FakeSdkClient.last_options = None
    FakeSdkClient.last_prompt = ""

    def _make_client(*, options: Any = None, **_: object) -> FakeSdkClient:
        FakeSdkClient.last_options = options
        return FakeSdkClient(messages, delay=delay)

    snapshot = task_usage if task_usage is not None else TaskUsage(turns=0, cost_usd=0.0)
    with (
        patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch.object(headless_mod, "ClaudeSDKClient", _make_client),
        patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
    ):
        yield FakeSdkClient
