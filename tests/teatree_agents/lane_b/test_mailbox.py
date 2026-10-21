import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.mailbox import build_mailbox_toolset
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient


def _tool(tools: Mapping[str, Tool[None]], name: str) -> Callable[..., Awaitable[Any]]:
    return cast("Callable[..., Awaitable[Any]]", tools[name].function)


def test_native_mailbox_tools_have_the_peer_names() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    toolset = build_mailbox_toolset(client, "coding")

    assert set(toolset.tools) == {
        "agent_mailbox_peers",
        "agent_mailbox_send",
        "agent_mailbox_inbox",
        "agent_mailbox_wait",
    }


def test_phase_grants_and_mcp_outage_do_not_remove_native_tools() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    all_four = {"agent_mailbox_peers", "agent_mailbox_send", "agent_mailbox_inbox", "agent_mailbox_wait"}
    with patch("teatree.agents.lane_b.mcp.mcp_client_available", return_value=False):
        coding = build_lane_b_toolsets(LaneBToolConfig(phase="coding", mailbox=client))
        reviewing = build_lane_b_toolsets(LaneBToolConfig(phase="reviewing", mailbox=client))
        empty = build_lane_b_toolsets(LaneBToolConfig(phase="short_describe", mailbox=client))
    assert set(coding.toolsets[-1].tools) == all_four
    assert set(reviewing.toolsets[-1].tools) == all_four
    assert not any(hasattr(toolset, "tools") and "agent_mailbox_peers" in toolset.tools for toolset in empty.toolsets)
    with patch("teatree.agents.lane_b.mailbox.mcp_write_tools_for_phase", return_value=frozenset()):
        assert set(build_mailbox_toolset(client, "reviewing").tools) == {"agent_mailbox_peers"}


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


def test_send_inbox_and_peers_round_trip_over_the_broker_socket(tmp_path: Path) -> None:
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        sender = broker.register(room="ticket", harness="pydantic_ai", label="sender")
        recipient = broker.register(room="ticket", harness="pydantic_ai", label="recipient")
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
