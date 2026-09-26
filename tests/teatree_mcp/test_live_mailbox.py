"""Live mailbox contract at the runner-to-MCP Unix-socket boundary."""

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from teatree.agents.live_mailbox import LiveMailboxBroker, bound_task_mailbox, reset_shared_brokers, shared_broker
from teatree.mcp.agent_mailbox import InboxPage, MailboxClient
from teatree.mcp.server import build_server
from tests.teatree_mcp._call_tool_result import structured

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpStdioServerConfig


def invoke(client: MailboxClient, method: str, **kwargs: Any) -> Any:
    return asyncio.run(getattr(client, method)(**kwargs))


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[LiveMailboxBroker]:
    with LiveMailboxBroker(runtime_dir=tmp_path) as running:
        yield running


def test_shared_broker_reset_revokes_participants_and_replaces_broker() -> None:
    first = shared_broker()
    identity = first.register(room="ticket", harness="codex", label="coder")

    reset_shared_brokers()

    assert not identity.socket_path.exists()
    assert shared_broker() is not first


def test_two_codex_tasks_exchange_messages_without_database(broker: LiveMailboxBroker) -> None:
    first = broker.register(room="ticket-1", harness="codex", label="coder-a")
    second = broker.register(room="ticket-1", harness="codex", label="coder-b")
    first_client = MailboxClient(first.socket_path, first.token)
    second_client = MailboxClient(second.socket_path, second.token)

    peers = invoke(first_client, "peers")
    assert {peer["address"] for peer in peers} == {second.address}
    sent = invoke(first_client, "send", to=second.address, text="Review this", key="review-1")
    inbox = invoke(second_client, "inbox", after_id=0)

    assert inbox["messages"] == [
        {
            "id": sent["id"],
            "from": first.address,
            "from_harness": "codex",
            "from_label": "coder-a",
            "text": "Review this",
        }
    ]
    assert invoke(first_client, "inbox", after_id=0)["messages"] == []


def test_identity_and_room_are_owned_by_runner(broker: LiveMailboxBroker) -> None:
    first = broker.register(room="ticket-1", harness="claude", label="planner")
    second = broker.register(room="ticket-2", harness="codex", label="coder")
    first_client = MailboxClient(first.socket_path, first.token)
    assert {peer["address"] for peer in invoke(first_client, "peers")} == set()

    with pytest.raises(ToolError, match="outside this room"):
        invoke(first_client, "send", to=second.address, text="not for you", key="cross-room")
    with pytest.raises(ToolError, match="Invalid mailbox identity"):
        invoke(MailboxClient(first.socket_path, "bogus"), "peers")


def test_message_retries_and_limits(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="planner")
    recipient = broker.register(room="ticket", harness="codex", label="coder")
    client = MailboxClient(sender.socket_path, sender.token)

    first = invoke(client, "send", to=recipient.address, text="ping", key="once")
    assert invoke(client, "send", to=recipient.address, text="ping", key="once") == first
    with pytest.raises(ToolError, match="already used"):
        invoke(client, "send", to=recipient.address, text="changed", key="once")
    with pytest.raises(ToolError, match="16384 bytes"):
        invoke(client, "send", to=recipient.address, text="🙂" * 4097, key="too-large")


def test_wait_is_bounded_and_unregister_discards_mail(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="planner")
    recipient = broker.register(room="ticket", harness="codex", label="coder")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)

    async def exchange() -> InboxPage:
        waiting = asyncio.create_task(recipient_client.wait(after_id=0, timeout_seconds=2))
        await asyncio.sleep(0.05)
        await sender_client.send(to=recipient.address, text="ready", key="ready")
        return await waiting

    assert asyncio.run(exchange())["messages"][0]["text"] == "ready"
    with pytest.raises(ToolError, match="timeout"):
        invoke(recipient_client, "wait", after_id=0, timeout_seconds=3600)

    broker.unregister(recipient.address)
    with pytest.raises(ToolError, match="Invalid mailbox identity"):
        invoke(recipient_client, "inbox", after_id=0)
    with pytest.raises(ToolError, match="not active"):
        invoke(sender_client, "send", to=recipient.address, text="late", key="late")


def test_revoked_sender_cannot_send_after_token_lookup(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="planner")
    recipient = broker.register(room="ticket", harness="codex", label="coder")
    captured = broker._by_token[sender.token]
    broker.unregister(sender.address)

    with pytest.raises(ValueError, match="Invalid mailbox identity"):
        broker._send(captured, {"to": recipient.address, "text": "late", "key": "late"})


def test_runner_binding_exposes_only_its_own_mcp_identity(broker: LiveMailboxBroker) -> None:
    options = ClaudeAgentOptions(mcp_servers={"teatree": {"type": "stdio", "command": "t3", "args": ["mcp", "serve"]}})
    with bound_task_mailbox(options, room="ticket-1", harness="codex", label="coder", broker=broker) as identity:
        assert identity is not None
        assert isinstance(options.mcp_servers, dict)
        teatree = cast("McpStdioServerConfig", options.mcp_servers["teatree"])
        env = teatree["env"]
        assert env["T3_AGENT_MAILBOX_TOKEN"] == identity.token
        with patch.dict("os.environ", env):
            server = build_server()
            names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert "agent_mailbox_self" in names
        assert "agent_mailbox_send" in names

    assert isinstance(options.mcp_servers, dict)
    teatree = cast("McpStdioServerConfig", options.mcp_servers["teatree"])
    assert "env" not in teatree
    with pytest.raises(ToolError, match="Invalid mailbox identity"):
        invoke(MailboxClient(identity.socket_path, identity.token), "peers")


def test_a_broker_that_cannot_register_leaves_the_dispatch_unbound(caplog: pytest.LogCaptureFixture) -> None:
    """The mailbox is a convenience of the run; a broken broker must not fail every dispatch."""

    class _BrokenBroker(LiveMailboxBroker):
        def register(self, *, room: str, harness: str, label: str) -> NoReturn:
            msg = "socket dir unwritable"
            raise OSError(msg)

    servers = {"teatree": {"type": "stdio", "command": "t3", "args": ["mcp", "serve"]}}
    options = ClaudeAgentOptions(mcp_servers=servers)
    with bound_task_mailbox(
        options, room="ticket-1", harness="codex", label="coder", broker=_BrokenBroker()
    ) as identity:
        assert identity is None
        assert options.mcp_servers == servers
    assert "mailbox" in caplog.text


def test_interleaved_room_mail_does_not_report_false_truncation(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    first = broker.register(room="ticket", harness="codex", label="first")
    second = broker.register(room="ticket", harness="codex", label="second")
    client = MailboxClient(sender.socket_path, sender.token)
    invoke(client, "send", to=first.address, text="one", key="one")
    invoke(client, "send", to=second.address, text="two", key="two")

    inbox = invoke(MailboxClient(second.socket_path, second.token), "inbox", after_id=0)

    assert inbox["messages"][0]["text"] == "two"
    assert inbox["truncated"] is False


def test_inbox_can_return_a_page_of_large_but_valid_messages(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)
    body = "x" * 16_000
    for index in range(5):
        invoke(sender_client, "send", to=recipient.address, text=body, key=f"large-{index}")

    inbox = invoke(recipient_client, "inbox")
    assert len(inbox["messages"]) == 5
    assert all(message["text"] == body for message in inbox["messages"])


def test_escaped_body_within_byte_limit_is_delivered(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)
    body = "\x00" * 16_384

    invoke(sender_client, "send", to=recipient.address, text=body, key="escaped")

    assert invoke(recipient_client, "inbox")["messages"][0]["text"] == body


def test_escaped_mailbox_pages_stay_within_transport_limit(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)
    body = "\x00" * 16_384

    async def exchange() -> list[str]:
        for index in range(100):
            await sender_client.send(recipient.address, body, f"escaped-{index}")
        texts: list[str] = []
        cursor = 0
        while len(texts) < 100:
            page = await recipient_client.inbox(after_id=cursor, limit=100)
            assert page["messages"]
            texts.extend(message["text"] for message in page["messages"])
            cursor = page["next_cursor"]
        return texts

    assert asyncio.run(exchange()) == [body] * 100


def test_claude_and_codex_stdio_mcp_children_exchange_live_mail(tmp_path: Path, broker: LiveMailboxBroker) -> None:
    script = tmp_path / "mailbox_mcp_child.py"
    script.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "from teatree.mcp.agent_mailbox import register\n"
        "server = MCPServer('teatree-mailbox-test')\n"
        "assert register(server)\n"
        "server.run('stdio')\n"
    )
    claude = broker.register(room="ticket", harness="claude_sdk", label="planner")
    codex = broker.register(room="ticket", harness="codex_app_server", label="coder")
    claude_child = StdioServerParameters(command=sys.executable, args=[str(script)], env=claude.mcp_env())
    codex_child = StdioServerParameters(command=sys.executable, args=[str(script)], env=codex.mcp_env())

    async def exchange() -> tuple[dict[str, Any], dict[str, Any]]:
        async with (
            stdio_client(claude_child) as (claude_read, claude_write),
            stdio_client(codex_child) as (codex_read, codex_write),
            ClientSession(claude_read, claude_write) as claude_session,
            ClientSession(codex_read, codex_write) as codex_session,
        ):
            await asyncio.gather(claude_session.initialize(), codex_session.initialize())
            sent = await claude_session.call_tool(
                "agent_mailbox_send",
                {"to": codex.address, "text": "please review", "key": "stdio-once"},
            )
            inbox = await codex_session.call_tool("agent_mailbox_inbox", {"after_id": 0})
            return cast("dict[str, Any]", structured(sent)), cast("dict[str, Any]", structured(inbox))

    sent, inbox = asyncio.run(exchange())
    assert sent["to"] == codex.address
    assert inbox["messages"][0]["text"] == "please review"
