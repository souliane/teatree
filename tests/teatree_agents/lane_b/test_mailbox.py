import asyncio
from pathlib import Path
from unittest.mock import patch

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.mailbox import build_mailbox_toolset
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from teatree.agents.live_mailbox_client import MailboxClient


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
    with patch("teatree.agents.lane_b.mcp.mcp_client_available", return_value=False):
        coding = build_lane_b_toolsets(LaneBToolConfig(phase="coding", mailbox=client))
        reviewing = build_lane_b_toolsets(LaneBToolConfig(phase="reviewing", mailbox=client))
        empty = build_lane_b_toolsets(LaneBToolConfig(phase="short_describe", mailbox=client))
    assert set(coding.toolsets[-1].tools) == {
        "agent_mailbox_peers",
        "agent_mailbox_send",
        "agent_mailbox_inbox",
        "agent_mailbox_wait",
    }
    assert set(reviewing.toolsets[-1].tools) == {"agent_mailbox_peers"}
    assert not any(hasattr(toolset, "tools") and "agent_mailbox_peers" in toolset.tools for toolset in empty.toolsets)
    with patch(
        "teatree.agents.lane_b.mailbox.mcp_write_tools_for_phase",
        return_value=frozenset({"agent_mailbox_send", "agent_mailbox_inbox", "agent_mailbox_wait"}),
    ):
        assert set(build_mailbox_toolset(client, "reviewing").tools) == set(coding.toolsets[-1].tools)


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
