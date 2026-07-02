"""Shared in-process harness test doubles for the headless runner.

``run_headless`` drives an in-process agent session behind the ``Harness`` seam
(:mod:`teatree.agents.harness`). :class:`FakeHarnessSession` is the session double
— it IS the session surface (``query`` / ``receive_response`` / ``interrupt``),
yielding a canned typed-message stream. Two ways to inject it.

:func:`fake_sdk` patches the SDK boundary INSIDE the default backend
(``teatree.agents.harness.ClaudeSDKClient``), so a run flows through the REAL
:class:`~teatree.agents.harness.ClaudeSdkHarness` — proving the transport is
behaviour-preserving through the seam. :class:`FakeHarness` is a pure ``Harness``
double a test can inject directly (e.g. into ``_drive_with_heartbeat``) to
exercise the seam with no SDK at all.

``fake_sdk`` also stubs ``TaskUsage.for_task`` to a static snapshot: the runner
samples accumulated deltas via ``asyncio.to_thread`` before the run, and a
threaded DB read under ``TestCase``'s wrapping SQLite transaction is a harness
artifact that deadlocks the connection — not production behaviour.
"""

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, Self
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, TextBlock
from claude_agent_sdk.types import RateLimitInfo, RateLimitStatus, RateLimitType

import teatree.agents.harness as harness_mod
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


class FakeHarnessSession:
    """The session double — the ``HarnessSession`` surface over a canned stream.

    Records the ``options`` its owning harness was opened with on the class so a
    test can assert what the runner passed (model, cwd, resume).
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


class FakeHarness:
    """A pure ``Harness`` double — opens a :class:`FakeHarnessSession`.

    Records the options it was opened with so a seam test can assert the driver
    passed the built options straight through to the backend.
    """

    def __init__(self, messages: list[Any], *, delay: float = 0.0) -> None:
        self._messages = messages
        self._delay = delay
        self.opened_options: Any = None

    @contextlib.asynccontextmanager
    async def open(self, options: Any) -> AsyncIterator[FakeHarnessSession]:
        self.opened_options = options
        yield FakeHarnessSession(self._messages, delay=self._delay)


@contextlib.contextmanager
def fake_sdk(
    messages: list[Any],
    *,
    delay: float = 0.0,
    task_usage: TaskUsage | None = None,
) -> Iterator[type[FakeHarnessSession]]:
    """Patch the default backend's SDK boundary (+ ``TaskUsage.for_task``).

    The patch target is ``teatree.agents.harness.ClaudeSDKClient`` — the SDK the
    real :class:`~teatree.agents.harness.ClaudeSdkHarness` constructs — so a run
    still resolves and flows through that backend, only the transport is faked.
    """
    FakeHarnessSession.last_options = None
    FakeHarnessSession.last_prompt = ""

    def _make_client(*, options: Any = None, **_: object) -> FakeHarnessSession:
        FakeHarnessSession.last_options = options
        return FakeHarnessSession(messages, delay=delay)

    snapshot = task_usage if task_usage is not None else TaskUsage(turns=0, cost_usd=0.0)
    with (
        patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch.object(harness_mod, "ClaudeSDKClient", _make_client),
        patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
    ):
        yield FakeHarnessSession
