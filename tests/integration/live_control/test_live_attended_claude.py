"""Opt-in real Claude Code session attached through the plugin's ``t3 mcp serve --interactive``."""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage, query
from django.test import TestCase

from teatree.agents.live_delivery import MAX_WAIT_SECONDS, InboxPage
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.core.mcp_registration import TEATREE_MCP_SERVER_NAME
from tests.integration.live_control.live_proof import options
from tests.teatree_cli.test_cli_mcp import _WORKER_SCRIPT, _clean_env, _migrate

_ATTENDED_SERVER = "from teatree.cli.mcp import serve; serve(interactive=True)"
_PEER_WAITS = 6


async def _first_message(peer: MailboxClient) -> InboxPage:
    page: InboxPage = {"messages": [], "next_cursor": 0, "truncated": False}
    for _ in range(_PEER_WAITS):
        page = await peer.wait(timeout_seconds=MAX_WAIT_SECONDS)
        if page["messages"]:
            break
    return page


@pytest.mark.integration
@pytest.mark.timeout(360)
class LiveAttendedClaudeTests(TestCase):
    def test_a_real_claude_session_on_the_plugin_server_exchanges_nonces_with_a_worker_peer(self) -> None:
        if os.environ.get("T3_LIVE_INTERACTIVE") != "1":
            self.skipTest("set T3_LIVE_INTERACTIVE=1 for the attended live proof")
        nonce_to_peer, nonce_to_attended = secrets.token_hex(12), secrets.token_hex(12)
        with tempfile.TemporaryDirectory(prefix="t3m-", dir="/tmp") as root:
            directory = Path(root)
            env = _clean_env(directory / "xdg")
            env["T3_CONTROL_DB_DIR"] = str(directory / "control")
            env["T3_MCP_DELEGATED"] = "1"
            _migrate(env)
            worker = subprocess.Popen(
                [sys.executable, "-c", _WORKER_SCRIPT, str(directory / "control" / "live"), "t3", "worker"],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert worker.stdout is not None
                registered = json.loads(worker.stdout.readline())
                peer = MailboxClient(Path(registered["socket"]), registered["token"])
                attended_options = options("claude_sdk", "coding", str(directory))
                attended_options.mcp_servers = {
                    TEATREE_MCP_SERVER_NAME: {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": ["-c", _ATTENDED_SERVER],
                        "env": env,
                    }
                }
                prompt = (
                    "Use the TeaTree mailbox peers tool to find the live peer labelled 'worker-agent' "
                    f"(address {registered['address']}), then send it exactly '{nonce_to_peer}' with a fresh key "
                    "using the mailbox send tool. Then call the mailbox wait tool until its reply arrives "
                    "(at most five calls) and include the reply in your final answer."
                )

                async def attend() -> str:
                    answer = ""
                    async for message in query(prompt=prompt, options=attended_options):
                        if isinstance(message, ResultMessage):
                            answer = message.result or ""
                    return answer

                async def converse() -> tuple[str, str]:
                    received = (await _first_message(peer))["messages"][0]
                    reply = await peer.send(received["from"], nonce_to_attended, "reply-1")
                    return received["text"], reply["outcome"]

                async def exchange() -> tuple[str, tuple[str, str]]:
                    return await asyncio.gather(attend(), converse())

                attended_answer, (received_text, reply_outcome) = asyncio.run(exchange())
            finally:
                assert worker.stdin is not None
                worker.stdin.write("\n")
                worker.stdin.flush()
                worker.communicate(timeout=10)
        assert received_text == nonce_to_peer
        assert reply_outcome == "accepted_inbox_read"
        assert nonce_to_attended in attended_answer
