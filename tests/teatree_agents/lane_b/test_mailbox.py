import asyncio
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.mailbox import build_mailbox_toolset
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from teatree.agents.live_control import LiveTask
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.core.answering.escalation import KINDS
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from tests.factories import SessionFactory, TaskFactory, TicketFactory


def _tool(tools: Mapping[str, Tool[None]], name: str) -> Callable[..., Awaitable[Any]]:
    return cast("Callable[..., Awaitable[Any]]", tools[name].function)


def test_phase_grants_and_mcp_outage_do_not_remove_native_tools() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    granted = {"agent_mailbox_peers", "agent_mailbox_send", "agent_mailbox_inbox", "agent_mailbox_wait", "escalate"}
    assert set(build_mailbox_toolset(client, "coding").tools) == granted
    with patch("teatree.agents.lane_b.mcp.mcp_client_available", return_value=False):
        coding = build_lane_b_toolsets(LaneBToolConfig(phase="coding", mailbox=client))
        reviewing = build_lane_b_toolsets(LaneBToolConfig(phase="reviewing", mailbox=client))
        empty = build_lane_b_toolsets(LaneBToolConfig(phase="short_describe", mailbox=client))
    assert set(coding.toolsets[-1].tools) == granted
    assert set(reviewing.toolsets[-1].tools) == granted
    assert not any(hasattr(toolset, "tools") and "agent_mailbox_peers" in toolset.tools for toolset in empty.toolsets)
    with patch("teatree.agents.lane_b.mailbox.mcp_write_tools_for_phase", return_value=frozenset()):
        assert set(build_mailbox_toolset(client, "reviewing").tools) == {"agent_mailbox_peers"}


def test_only_a_phase_granted_escalate_holds_the_native_tool() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")

    granted = build_mailbox_toolset(client, "planning").tools["escalate"]
    assert all(kind in (granted.description or "") for kind in KINDS)
    assert "escalate" not in build_mailbox_toolset(client, "answering").tools
    assert "escalate" not in build_mailbox_toolset(client, "requesting_review").tools


def test_native_send_explains_how_to_retry_without_a_second_copy() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    toolset = build_mailbox_toolset(client, "coding")
    schema = toolset.tools["agent_mailbox_send"].function_schema.json_schema
    observed: list[str] = []

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        observed.append(repr(messages))
        return ModelResponse(parts=[TextPart(content="done")])

    agent: Agent[None, str] = Agent(FunctionModel(respond), toolsets=[toolset])
    assert asyncio.run(agent.run("go")).output == "done"

    assert "retry with the same key" in observed[0]
    assert "same key" in schema["properties"]["key"]["description"]


def test_a_broker_outage_returns_tool_data_and_the_agent_finishes() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    observed: list[str] = []

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if len(observed) == 0:
            observed.append("first")
            return ModelResponse(parts=[ToolCallPart(tool_name="agent_mailbox_peers", args={}, tool_call_id="c1")])
        results = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        observed.append(repr(results))
        return ModelResponse(parts=[TextPart(content="done")])

    agent: Agent[None, str] = Agent(FunctionModel(respond), toolsets=[build_mailbox_toolset(client, "coding")])
    assert asyncio.run(agent.run("go")).output == "done"
    assert "Live mailbox is unavailable" in observed[1]


def test_an_oversized_send_returns_tool_data_and_the_agent_finishes(tmp_path: Path) -> None:
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        sender = broker.register(room="ticket", harness="pydantic_ai", label="sender")
        client = MailboxClient(sender.socket_path, sender.token)
        observed: list[object] = []

        def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            if not observed:
                observed.append("first")
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="agent_mailbox_send",
                            args={"to": "absent", "text": "x" * 400_000, "key": "retry-key"},
                            tool_call_id="c1",
                        )
                    ]
                )
            results = [
                part
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            assert len(results) == 1
            observed.append(results[0].content)
            return ModelResponse(parts=[TextPart(content="done")])

        with patch("teatree.agents.lane_b.mcp.mcp_client_available", return_value=False):
            toolsets = build_lane_b_toolsets(LaneBToolConfig(phase="coding", mailbox=client)).toolsets
        agent: Agent[None, str] = Agent(FunctionModel(respond), toolsets=toolsets)
        assert asyncio.run(agent.run("go")).output == "done"
        assert observed[1] in (
            {"error": "Live mailbox is unavailable; this TeaTree session may have ended"},
            {"error": "Separator is not found, and chunk exceed the limit"},
        )


@pytest.mark.parametrize(
    "reply",
    [b"garbage\n", b"x" * 2_000_001 + b"\n", b"\xff\n", b"[" * 10_000 + b"0" + b"]" * 10_000 + b"\n"],
    ids=["malformed-json", "oversized-line", "invalid-utf8", "deeply-nested-json"],
)
def test_a_bad_broker_reply_is_tool_data(tmp_path: Path, reply: bytes) -> None:
    async def run() -> object:
        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(reply)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        socket = tmp_path / "mailbox.sock"
        server = await asyncio.start_unix_server(answer, path=str(socket))
        async with server:
            client = MailboxClient(socket, "private-token")
            return await _tool(build_mailbox_toolset(client, "coding").tools, "agent_mailbox_peers")()

    assert asyncio.run(run()) == {"error": "Live mailbox returned an invalid response"}


def test_native_wait_calls_the_client_wait() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    page = {"messages": [], "next_cursor": 3, "truncated": False}
    with patch.object(client, "wait", new=AsyncMock(return_value=page)) as waiting:
        result = asyncio.run(
            _tool(build_mailbox_toolset(client, "coding").tools, "agent_mailbox_wait")(after_id=3, timeout_seconds=0.5)
        )
    assert result == page
    waiting.assert_awaited_once_with(after_id=3, timeout_seconds=0.5)


def test_send_inbox_and_peers_round_trip_over_the_broker_socket(tmp_path: Path) -> None:
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        sender = broker.register(room="ticket", harness="pydantic_ai", label="sender")
        recipient = broker.register(room="ticket", harness="pydantic_ai", label="recipient", can_receive=True)
        sending = build_mailbox_toolset(MailboxClient(sender.socket_path, sender.token), "coding").tools
        reading = build_mailbox_toolset(MailboxClient(recipient.socket_path, recipient.token), "coding").tools

        async def run() -> tuple[Any, Any, Any]:
            peers = await _tool(sending, "agent_mailbox_peers")()
            send = asyncio.create_task(
                _tool(sending, "agent_mailbox_send")(to=recipient.address, text="hello peer", key="k1")
            )
            deadline = time.monotonic() + 5
            page: Any = {"messages": []}
            while not page["messages"] and time.monotonic() < deadline:
                page = await _tool(reading, "agent_mailbox_inbox")()
            return peers, page, await asyncio.wait_for(send, timeout=5)

        peers, page, receipt = asyncio.run(run())

    assert [peer["address"] for peer in peers if peer["address"] == recipient.address] == [recipient.address]
    assert [message["text"] for message in page["messages"]] == ["hello peer"]
    assert receipt["outcome"] == "accepted_inbox_read"


def test_the_identity_never_reaches_the_tool_schemas_or_the_mcp_launch() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    with patch("teatree.agents.lane_b.toolsets.build_mcp_toolsets", return_value=[]) as mcp:
        toolsets = build_lane_b_toolsets(LaneBToolConfig(phase="coding", mailbox=client))
    mailbox_tools = toolsets.toolsets[-1].tools
    described = [(tool.description, tool.function_schema.json_schema) for tool in mailbox_tools.values()]

    assert "private-token" not in repr((described, mcp.call_args, client))


class TestAPydanticTaskEscalates(TransactionTestCase):
    def test_it_escalates_through_its_native_tool_and_a_refusal_is_tool_data(self) -> None:
        ticket = TicketFactory(state=Ticket.State.WORK_STARTED)
        asking = TaskFactory(
            ticket=ticket, session=SessionFactory(ticket=ticket), phase="planning", status=Task.Status.CLAIMED
        )
        parent = TaskFactory(
            ticket=ticket, session=SessionFactory(ticket=ticket), phase="answering", status=Task.Status.COMPLETED
        )
        live = LiveTask(pk=asking.pk, ticket=ticket.pk, phase="planning", harness="pydantic_ai", model="m")
        calls = [
            {"kind": "internal", "message": "Backfill before the migration?"},
            {"kind": "spend_money", "message": "Buy more capacity?"},
        ]
        returned: list[object] = []
        runtime = self.enterContext(tempfile.TemporaryDirectory())

        with LiveMailboxBroker(runtime_dir=Path(runtime)) as broker:
            identity = broker.register(
                room=str(ticket.pk), harness="pydantic_ai", label=f"planning task {asking.pk}", task=live
            )
            client = MailboxClient(identity.socket_path, identity.token)

            def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                results = [
                    part.content
                    for message in messages
                    if isinstance(message, ModelRequest)
                    for part in message.parts
                    if isinstance(part, ToolReturnPart)
                ]
                returned[:] = results
                if len(results) == len(calls):
                    return ModelResponse(parts=[TextPart(content="done")])
                return ModelResponse(
                    parts=[
                        ToolCallPart(tool_name="escalate", args=calls[len(results)], tool_call_id=f"c{len(results)}")
                    ]
                )

            toolset = build_mailbox_toolset(client, "planning")
            agent: Agent[None, str] = Agent(FunctionModel(respond), toolsets=[toolset])
            assert async_to_sync(agent.run)("go").output == "done"

        row = DeferredQuestion.objects.get()
        assert returned[0] == {"request_id": row.pk, "delivery": "resumed", "deadline_minutes": 30}
        assert isinstance(returned[1], dict)
        assert "valid kinds" in str(returned[1]["error"])
        assert Task.objects.get(parent_task=parent).phase == "answering"
