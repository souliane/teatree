"""Shared route, evidence and operator helpers for opt-in Pydantic mailbox proofs."""

import asyncio
import json
import os
import re
import sys
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self
from unittest.mock import patch

import httpx2 as httpx
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    ToolCallPart,
    ToolReturnPart,
)

from teatree.agents import pydantic_ai_config
from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.pydantic_ai_config import OpenAICompatibleLaneConfig, PydanticAiModelConfig
from teatree.agents.runner_heartbeat import HeartbeatRuntime
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.config.agent_enums import AgentHarness
from teatree.llm.credentials import CredentialError, CredentialSource, PassSource
from teatree.llm.openai_compatible import OpenAICompatibleCredential

LIVE_ROUTE_ENTRY_ENV = "T3_LIVE_PYDANTIC_CREDENTIAL_ENTRY"
LIVE_ROUTE_MODEL_ENV = "T3_LIVE_PYDANTIC_MODEL"
LIVE_ROUTE_BASE_URL = "https://openrouter.ai/api/v1"
LIVE_ROUTE_DEFAULT_MODEL = "openai/gpt-oss-120b"

OPERATOR_DEADLINE_SECONDS = 150.0
OPERATOR_JOIN_SECONDS = OPERATOR_DEADLINE_SECONDS + 90.0

PROVIDER_KEY_PREFIX = "sk-or-"
_PROVIDER_KEY = re.compile(re.escape(PROVIDER_KEY_PREFIX) + r"[A-Za-z0-9_-]+")
_ABSOLUTE_PATH = re.compile(r"(?<![\w:/<.-])/(?:[\w.-]+/)*[\w.-]+")
_TEXT_EXCERPT = 80
_AGENT_TEXT_EXCERPT = 160


def resolve_live_route(
    *, source: CredentialSource | None = None, context_windows: Mapping[str, int] | None = None
) -> PydanticAiModelConfig:
    entry = os.environ.get(LIVE_ROUTE_ENTRY_ENV, "").strip()
    if not entry:
        msg = f"set {LIVE_ROUTE_ENTRY_ENV} to the approved pass entry for Pydantic live proofs"
        raise CredentialError(msg)
    OpenAICompatibleCredential(
        sources=(source if source is not None else PassSource(),),
        pass_path_override=entry,
    ).resolve()
    return PydanticAiModelConfig(
        backend=OpenAICompatibleLaneConfig(
            base_url=LIVE_ROUTE_BASE_URL,
            credential_entry=entry,
            model=os.environ.get(LIVE_ROUTE_MODEL_ENV, "").strip() or LIVE_ROUTE_DEFAULT_MODEL,
            request_limit=6,
        ),
        max_tokens=512,
        context_windows=context_windows or {},
    )


def live_harness(
    config: PydanticAiModelConfig, *, history: list[ModelRequest | ModelResponse] | None = None
) -> GuardedHarness:
    return GuardedHarness(
        harness=PydanticAiHarness(history=history, phase="coding", config=config),
        guard=CompactionGuard(),
        name=AgentHarness.PYDANTIC_AI.value,
    )


@asynccontextmanager
async def closing_provider_clients() -> AsyncGenerator[None]:
    opened: list[httpx.AsyncClient] = []
    original_client = pydantic_ai_config.DefaultAsyncHttpxClient

    def recording_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = original_client(*args, **kwargs)
        opened.append(client)
        return client

    with patch.object(pydantic_ai_config, "DefaultAsyncHttpxClient", recording_client):
        try:
            yield
        finally:
            await asyncio.gather(*(client.aclose() for client in opened))


def live_runtime(*, idle_window_seconds: float = 0) -> HeartbeatRuntime:
    return HeartbeatRuntime(
        watchdog=LoopWatchdog(max_runtime_seconds=240, max_turns=0, max_cost_usd=0.0),
        heartbeat_interval=20,
        sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
        renew_lease=lambda _task: None,
        drain_reason=lambda: "",
        idle_window_seconds=idle_window_seconds,
    )


def masked(text: str) -> str:
    entry = os.environ.get(LIVE_ROUTE_ENTRY_ENV, "").strip()
    hidden = text.replace(entry, "<entry>") if entry else text
    return _ABSOLUTE_PATH.sub("<path>", _PROVIDER_KEY.sub("<key>", hidden))


def _return_summary(content: object) -> dict[str, object]:
    if isinstance(content, dict):
        return {"keys": sorted(content), "error": content.get("error"), "receipt_outcome": content.get("outcome")}
    if isinstance(content, str):
        return {"text": content[:_TEXT_EXCERPT]}
    if isinstance(content, list):
        return {"count": len(content)}
    return {"type": type(content).__name__}


def tool_trail(thread: Sequence[ModelMessage] | None) -> list[dict[str, object]]:
    trail: list[dict[str, object]] = []
    for message in thread or ():
        for part in message.parts:
            if isinstance(part, ToolCallPart) and isinstance(message, ModelResponse):
                args = part.args_as_dict()
                call: dict[str, object] = {"call": part.tool_name, "arg_keys": sorted(args)}
                if part.tool_name == "Bash":
                    call["command"] = args.get("command")
                trail.append(call)
            elif isinstance(part, ToolReturnPart) and isinstance(message, ModelRequest):
                trail.append({"return": part.tool_name, "outcome": part.outcome, **_return_summary(part.content)})
            elif isinstance(part, RetryFeedbackPart):
                trail.append({"retry_feedback": str(part.content)[:_TEXT_EXCERPT]})
    return trail


def _described(error: BaseException | None) -> str | None:
    return None if error is None else f"{type(error).__name__}: {str(error)[:300]}"


class Evidence:
    """One node's evidence line, written on exit whether the node passed, failed or raised."""

    def __init__(self, node: str, *, model: str) -> None:
        self.node = node
        self.model = model
        self.outcome: HarnessOutcome | BaseException | None = None
        self.facts: dict[str, object] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        sys.stdout.write(masked(json.dumps(self.line(exc), sort_keys=True, default=str)) + "\n")
        sys.stdout.flush()

    def line(self, failure: BaseException | None) -> dict[str, object]:
        outcome = self.outcome if isinstance(self.outcome, HarnessOutcome) else None
        result = outcome.result_message if outcome is not None else None
        return {
            "node": self.node,
            "model": self.model,
            "request_count": result.num_turns if result is not None else None,
            "cost_usd": result.total_cost_usd if result is not None else None,
            "paused": outcome.paused if outcome is not None else None,
            "tool_calls": outcome.tool_calls if outcome is not None else None,
            "agent_text": outcome.agent_text[:_AGENT_TEXT_EXCERPT] if outcome is not None else None,
            "tool_trail": tool_trail(outcome.thread) if outcome is not None else [],
            "drive_error": _described(self.outcome if isinstance(self.outcome, BaseException) else None),
            "failure": _described(failure),
            **self.facts,
        }


async def gather_settled[T, U](
    first: Awaitable[T], second: Awaitable[U]
) -> tuple[T | BaseException, U | BaseException]:
    return await asyncio.gather(first, second, return_exceptions=True)


class Operator:
    """A background actor that waits for a live condition, with a deadline and a stop signal."""

    def __init__(self, act: Callable[["Operator"], None]) -> None:
        self.seen: dict[str, Any] = {}
        self._drive_ended = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-operator")
        self._future = pool.submit(act, self)
        pool.shutdown(wait=False)

    def wait_for(self, ready: Callable[[], bool], *, what: str) -> bool:
        deadline = time.monotonic() + OPERATOR_DEADLINE_SECONDS
        while not ready():
            if self._drive_ended.is_set():
                self.seen["error"] = f"drive ended before {what}"
                return False
            if time.monotonic() >= deadline:
                self.seen["error"] = f"{what} never happened"
                return False
            self._drive_ended.wait(0.1)
        return True

    def stop(self) -> None:
        self._drive_ended.set()

    def finish(self) -> dict[str, Any]:
        self.stop()
        done, _ = wait([self._future], timeout=OPERATOR_JOIN_SECONDS)
        assert "error" not in self.seen, self.seen
        assert done, "operator is still running past its deadline"
        self._future.result()
        return self.seen


class PauseClient(Protocol):
    def pause(self, task: int, text: str, *, command_id: str, wait: float) -> Mapping[str, Any]: ...


def pause_when_marked(client: PauseClient, task_pk: int, marker: Path) -> Callable[[Operator], None]:
    def act(operator: Operator) -> None:
        if operator.wait_for(marker.exists, what="second Bash command starting"):
            operator.seen["receipt"] = client.pause(task_pk, "pause", command_id="pydantic-pause", wait=20)

    return act
