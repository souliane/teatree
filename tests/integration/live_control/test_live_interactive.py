"""Opt-in attended MCP peer and real Claude agent nonce exchange."""

import asyncio
import os
import secrets
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.live_mailbox import shared_broker
from teatree.agents.live_mailbox_client import InteractiveMailboxLease
from teatree.agents.runner_heartbeat import drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.core.models import Session, Task
from teatree.mcp.server import build_server
from tests.factories import planned_ticket
from tests.integration.live_control.live_proof import harness, options, runtime, wait_for_peer
from tests.teatree_mcp._call_tool_result import structured


@pytest.mark.integration
@pytest.mark.timeout(360)
class LiveInteractiveTests(TestCase):
    def test_attended_peer_and_real_agent_exchange_nonces_both_ways(self) -> None:
        if os.environ.get("T3_LIVE_INTERACTIVE") != "1":
            self.skipTest("set T3_LIVE_INTERACTIVE=1 for the attended live proof")
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        nonce_to_agent, nonce_to_attended = secrets.token_hex(12), secrets.token_hex(12)
        broker = shared_broker()
        with InteractiveMailboxLease.connect(broker.socket_path.parent) as attended:
            attended_env = attended.mcp_env()
            with patch.dict(os.environ, attended_env), patch("teatree.mcp.server.get_all_overlays", return_value={}):
                server = build_server()
            with ThreadPoolExecutor(max_workers=1) as pool:
                names = {tool.name for tool in asyncio.run(server.list_tools())}
                assert {f"agent_mailbox_{name}" for name in ("self", "peers", "send", "inbox", "wait")} <= names

                def receive_and_reply() -> HarnessOutcome:
                    return asyncio.run(
                        drive_with_heartbeat(
                            task,
                            "Use the TeaTree mailbox wait tool to read the attended peer's message. "
                            f"Then send exactly '{nonce_to_attended}' back to its address using the mailbox send "
                            "tool with a fresh key. Include the message you received in your final answer.",
                            options("claude_sdk", "coding", tempfile.mkdtemp(prefix="t3-live-interactive-")),
                            harness("claude_sdk"),
                            runtime=runtime(),
                        )
                    )

                agent_future = pool.submit(receive_and_reply)
                agent_address = wait_for_peer(task.pk, agent_future)

                async def exchange() -> tuple[dict[str, Any], dict[str, Any]]:
                    sending = asyncio.create_task(
                        server.call_tool(
                            "agent_mailbox_send", {"to": agent_address, "text": nonce_to_agent, "key": "attended-1"}
                        )
                    )
                    inbound = structured(
                        await server.call_tool("agent_mailbox_wait", {"after_id": 0, "timeout_seconds": 30})
                    )
                    return structured(await sending), inbound

                outbound, inbound = asyncio.run(exchange())
                agent = agent_future.result(timeout=150)
        assert outbound["outcome"] in {"accepted_current_turn", "accepted_inbox_read"}
        assert inbound["messages"][0]["text"] == nonce_to_attended
        assert nonce_to_agent in agent.agent_text
