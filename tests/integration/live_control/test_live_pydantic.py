"""The evidence, exchange and operator helpers behind the Pydantic live proofs run without a provider."""

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    RetryInfo,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from teatree.agents import pydantic_ai_config
from teatree.agents.pydantic_ai_config import OpenAICompatibleLaneConfig, PydanticAiModelConfig
from teatree.agents.runner_heartbeat import drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.integration.live_control import live_pydantic
from tests.integration.live_control.live_pydantic import (
    LIVE_ROUTE_ENTRY_ENV,
    OPERATOR_DEADLINE_SECONDS,
    OPERATOR_JOIN_SECONDS,
    Evidence,
    Operator,
    closing_provider_clients,
    gather_settled,
    live_harness,
    live_runtime,
    masked,
    pause_when_marked,
    tool_trail,
)

ENTRY = "tests/provider-free-entry"
PROVIDER_KEY = "sk-or-" + "v1-0123abcd_EF-45"
STREAM_CHUNKS = (
    {"delta": {"role": "assistant", "content": "done"}, "finish_reason": None},
    {"delta": {}, "finish_reason": "stop"},
)
PROVIDER_STREAM = (
    "".join(
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, **chunk}],
            }
        )
        + "\n\n"
        for chunk in STREAM_CHUNKS
    )
    + "data: [DONE]\n\n"
)


def _sender_thread() -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        ModelResponse(parts=[ToolCallPart("agent_mailbox_peers", {}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart("agent_mailbox_peers", [{"address": "peer-address"}], tool_call_id="c1")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    "agent_mailbox_send",
                    {"to": "peer-address", "text": "secret-nonce", "key": "k1"},
                    tool_call_id="c2",
                )
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("agent_mailbox_send", {"error": "unknown peer"}, tool_call_id="c2")]),
    ]


def test_tool_trail_names_calls_returns_and_retries_without_argument_values() -> None:
    thread = [
        *_sender_thread(),
        ModelResponse(parts=[ToolCallPart("Bash", {"command": "echo hello"}, tool_call_id="c3")]),
        ModelRequest(parts=[ToolReturnPart("Bash", "command refused", tool_call_id="c3", outcome="retried")]),
        ModelRequest(parts=[RetryFeedbackPart(content="response had no output", retry=RetryInfo(cause="no_output"))]),
    ]

    trail = tool_trail(thread)

    assert [step.get("call") or step.get("return") or "feedback" for step in trail] == [
        "agent_mailbox_peers",
        "agent_mailbox_peers",
        "agent_mailbox_send",
        "agent_mailbox_send",
        "Bash",
        "Bash",
        "feedback",
    ]
    assert trail[2]["arg_keys"] == ["key", "text", "to"]
    assert trail[3]["error"] == "unknown peer"
    assert trail[3]["outcome"] == "success"
    assert trail[4]["command"] == "echo hello"
    assert trail[5]["outcome"] == "retried"
    assert trail[6]["retry_feedback"] == "response had no output"
    assert "peer-address" not in repr(trail)
    assert "secret-nonce" not in repr(trail)


def test_masked_hides_the_entry_name_and_provider_keys() -> None:
    with patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: ENTRY}):
        text = masked(f"entry {ENTRY} key {PROVIDER_KEY} again {PROVIDER_KEY}")
    assert ENTRY not in text
    assert "sk-or-" not in text


def test_evidence_line_is_written_when_the_node_body_fails(capsys: pytest.CaptureFixture[str]) -> None:
    def failing_node() -> None:
        with Evidence("sender", model="model-x") as evidence:
            evidence.facts["inbox_nonce"] = False
            msg = "Pydantic did not send the nonce"
            raise AssertionError(msg)

    with pytest.raises(AssertionError, match="did not send"):
        failing_node()

    line = json.loads(capsys.readouterr().out)
    assert line["node"] == "sender"
    assert line["model"] == "model-x"
    assert line["inbox_nonce"] is False
    assert "did not send the nonce" in line["failure"]


def test_evidence_line_never_carries_the_entry_name_or_a_provider_key(capsys: pytest.CaptureFixture[str]) -> None:
    def failing_node() -> None:
        with Evidence("nonce", model="model-x"):
            msg = f"provider refused {PROVIDER_KEY} for {ENTRY}"
            raise RuntimeError(msg)

    with patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: ENTRY}), pytest.raises(RuntimeError):
        failing_node()

    out = capsys.readouterr().out
    assert out.strip()
    assert ENTRY not in out
    assert "sk-or-" not in out


def test_sender_evidence_keeps_the_drive_trail_when_the_receive_side_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    drive_outcome = HarnessOutcome(
        agent_text="done", result_message=None, stuck_reason=None, thread=_sender_thread(), tool_calls=2
    )

    async def drive() -> HarnessOutcome:
        await asyncio.sleep(0.01)
        return drive_outcome

    async def receive() -> None:
        await asyncio.sleep(0)
        msg = "Pydantic did not send the nonce"
        raise AssertionError(msg)

    def sender_node() -> None:
        with Evidence("sender", model="model-x") as evidence:
            drive_result, message = asyncio.run(gather_settled(drive(), receive()))
            evidence.outcome = drive_result
            if isinstance(message, BaseException):
                raise message

    with pytest.raises(AssertionError, match="did not send"):
        sender_node()

    line = json.loads(capsys.readouterr().out)
    assert line["tool_calls"] == 2
    assert [step["call"] for step in line["tool_trail"] if "call" in step] == [
        "agent_mailbox_peers",
        "agent_mailbox_send",
    ]
    assert line["tool_trail"][-1]["error"] == "unknown peer"


def test_evidence_line_names_a_failed_drive(capsys: pytest.CaptureFixture[str]) -> None:
    async def drive() -> HarnessOutcome:
        await asyncio.sleep(0)
        msg = "400 not a valid model ID"
        raise RuntimeError(msg)

    async def receive() -> None:
        await asyncio.sleep(0)

    with Evidence("sender", model="model-x") as evidence:
        drive_result, _ = asyncio.run(gather_settled(drive(), receive()))
        evidence.outcome = drive_result

    line = json.loads(capsys.readouterr().out)
    assert line["drive_error"] == "RuntimeError: 400 not a valid model ID"
    assert line["tool_trail"] == []


def test_operator_names_the_unmet_condition_before_it_names_a_stuck_thread() -> None:
    errored = threading.Event()
    release = threading.Event()

    def act(operator: Operator) -> None:
        operator.seen["error"] = "second Bash tool never opened"
        errored.set()
        release.wait(5)

    operator = Operator(act)
    assert errored.wait(5)
    try:
        with (
            patch.object(live_pydantic, "OPERATOR_JOIN_SECONDS", 0.05),
            pytest.raises(AssertionError, match="second Bash tool never opened"),
        ):
            operator.finish()
    finally:
        release.set()


def test_operator_stops_waiting_as_soon_as_the_drive_ends() -> None:
    def act(operator: Operator) -> None:
        operator.wait_for(lambda: False, what="idle window opening")

    with patch.object(live_pydantic, "OPERATOR_DEADLINE_SECONDS", 0.5):
        operator = Operator(act)
        operator.stop()
        with pytest.raises(AssertionError, match="drive ended before idle window opening"):
            operator.finish()


def test_operator_hands_back_what_it_saw() -> None:
    def act(operator: Operator) -> None:
        if operator.wait_for(lambda: True, what="anything"):
            operator.seen["receipt"] = {"outcome": "paused"}

    assert Operator(act).finish() == {"receipt": {"outcome": "paused"}}


def test_operator_reraises_an_unexpected_failure_of_its_actor() -> None:
    def act(operator: Operator) -> None:
        operator.seen.clear()
        msg = "receipt missing"
        raise KeyError(msg)

    with pytest.raises(KeyError, match="receipt missing"):
        Operator(act).finish()


def test_every_provider_client_made_during_a_drive_is_closed_after_it() -> None:
    original_client = pydantic_ai_config.DefaultAsyncHttpxClient
    made: list[httpx.AsyncClient] = []

    async def drive() -> None:
        async with closing_provider_clients():
            made.extend(pydantic_ai_config.DefaultAsyncHttpxClient() for _ in range(2))
            assert not any(client.is_closed for client in made)

    asyncio.run(drive())

    assert len(made) == 2
    assert all(client.is_closed for client in made)
    assert pydantic_ai_config.DefaultAsyncHttpxClient is original_client


def test_provider_clients_are_closed_when_the_drive_raises() -> None:
    made: list[httpx.AsyncClient] = []

    async def drive() -> None:
        async with closing_provider_clients():
            made.append(pydantic_ai_config.DefaultAsyncHttpxClient())
            msg = "drive failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="drive failed"):
        asyncio.run(drive())

    assert made[0].is_closed


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.paused = threading.Event()

    def pause(self, task: int, text: str, *, command_id: str, wait: float) -> dict[str, str]:
        self.calls.append((task, command_id))
        self.paused.set()
        return {"outcome": "paused"}


def test_pause_is_sent_once_the_second_command_has_started(tmp_path: Path) -> None:
    client = _RecordingClient()
    marker = tmp_path / "second-bash"
    operator = Operator(pause_when_marked(client, 7, marker))
    assert not client.paused.wait(0.3)

    marker.touch()

    assert client.paused.wait(5)
    assert operator.finish() == {"receipt": {"outcome": "paused"}}
    assert client.calls == [(7, "pydantic-pause")]


def test_pause_is_not_sent_while_the_second_command_has_not_started(tmp_path: Path) -> None:
    client = _RecordingClient()
    operator = Operator(pause_when_marked(client, 7, tmp_path / "second-bash"))
    operator.stop()

    with pytest.raises(AssertionError, match="drive ended before second Bash command starting"):
        operator.finish()
    assert client.calls == []


def test_operator_prefers_a_met_condition_over_the_end_of_the_drive() -> None:
    condition_met = threading.Event()
    drive_ended_first = threading.Event()

    def act(operator: Operator) -> None:
        assert drive_ended_first.wait(5)
        if operator.wait_for(condition_met.is_set, what="the condition"):
            operator.seen["acted"] = True

    operator = Operator(act)
    condition_met.set()
    operator.stop()
    drive_ended_first.set()

    assert operator.finish() == {"acted": True}


def test_operator_join_outlasts_its_deadline_and_the_final_blocking_calls() -> None:
    assert OPERATOR_JOIN_SECONDS >= OPERATOR_DEADLINE_SECONDS + 60


class LiveHarnessMailboxTests(TestCase):
    def test_the_model_is_offered_the_mailbox_tools_of_a_dispatched_pydantic_task(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        offered: list[set[str]] = []

        def provider(request: httpx.Request) -> httpx.Response:
            offered.append({tool["function"]["name"] for tool in json.loads(request.content).get("tools", [])})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=PROVIDER_STREAM)

        original_client = pydantic_ai_config.DefaultAsyncHttpxClient

        def stubbed_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return original_client(*args, transport=httpx.MockTransport(provider), **kwargs)

        route = PydanticAiModelConfig(
            backend=OpenAICompatibleLaneConfig(base_url="http://provider.invalid/v1", model="m", request_limit=6),
            max_tokens=64,
        )
        with (
            patch.dict(os.environ, {"OPENAI_COMPATIBLE_API_KEY": "provider-free-key"}),
            patch.object(pydantic_ai_config, "DefaultAsyncHttpxClient", stubbed_client),
        ):
            outcome = asyncio.run(
                drive_with_heartbeat(
                    task, "hello", ClaudeAgentOptions(max_turns=2), live_harness(route), runtime=live_runtime()
                )
            )

        assert outcome.agent_text == "done"
        assert {"agent_mailbox_peers", "agent_mailbox_send", "Bash"} <= offered[0]
