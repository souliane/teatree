"""Live mailbox contract at the runner-to-MCP Unix-socket boundary."""

import asyncio
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from teatree.agents import live_mailbox
from teatree.agents.live_control import LiveTask
from teatree.agents.live_mailbox import (
    LiveMailboxBroker,
    MailboxIdentity,
    bound_task_mailbox,
    reset_shared_brokers,
    shared_broker,
)
from teatree.agents.live_registry import LiveSessionRegistry
from teatree.mcp.agent_mailbox import InboxPage, MailboxClient, SendReceipt
from teatree.mcp.server import build_server
from tests.teatree_agents._live_owner import BoundarySession, OwnerLoop
from tests.teatree_mcp._call_tool_result import structured

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpStdioServerConfig


_TASK = LiveTask(pk=7, ticket=3, phase="coding", harness="claude_sdk", model="m")


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

    async def exchange() -> tuple[SendReceipt, InboxPage]:
        sending = asyncio.create_task(first_client.send(second.address, "Review this", "review-1"))
        inbox = await second_client.wait(timeout_seconds=2)
        return await sending, inbox

    sent, inbox = asyncio.run(exchange())

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


def _rejected(message_id: int, to: str, code: str, *, queued: bool) -> SendReceipt:
    return {"id": message_id, "to": to, "outcome": "rejected", "code": code, "queued_in_inbox": queued}


def _until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.fixture
def registry() -> LiveSessionRegistry:
    return LiveSessionRegistry()


@pytest.fixture
def routed_broker(tmp_path: Path, registry: LiveSessionRegistry) -> Iterator[LiveMailboxBroker]:
    with LiveMailboxBroker(runtime_dir=tmp_path, registry=lambda: registry) as running:
        yield running


def test_send_is_accepted_only_after_the_recipient_reads(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)

    async def exchange() -> tuple[bool, SendReceipt]:
        delivery = asyncio.create_task(sender_client.send(recipient.address, "review", "ack-1"))
        await asyncio.sleep(0.05)
        before_read = delivery.done()
        await recipient_client.inbox()
        receipt = await asyncio.wait_for(delivery, timeout=1)
        return before_read, receipt

    before_read, receipt = asyncio.run(exchange())
    assert before_read is False
    assert receipt == {
        "id": receipt["id"],
        "to": recipient.address,
        "outcome": "accepted_inbox_read",
        "code": None,
        "queued_in_inbox": False,
    }


def test_a_send_nobody_accepts_in_time_is_rejected_and_stays_queued(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    client = MailboxClient(sender.socket_path, sender.token)

    receipt = asyncio.run(client.send(recipient.address, "review", "ack-timeout", timeout_seconds=0.05))

    assert receipt == _rejected(receipt["id"], recipient.address, "not_accepted_in_time", queued=True)
    inbox = invoke(MailboxClient(recipient.socket_path, recipient.token), "inbox")
    assert [row["id"] for row in inbox["messages"]] == [receipt["id"]]


def test_a_timed_out_send_retried_with_its_key_is_accepted_without_a_second_message(
    broker: LiveMailboxBroker,
) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)

    async def exchange() -> tuple[SendReceipt, SendReceipt, InboxPage]:
        first = await sender_client.send(recipient.address, "review", "once", timeout_seconds=0.05)
        retry = asyncio.create_task(sender_client.send(recipient.address, "review", "once", timeout_seconds=1))
        page = await recipient_client.inbox()
        return first, await retry, page

    first, retried, page = asyncio.run(exchange())
    assert (first["outcome"], first["code"]) == ("rejected", "not_accepted_in_time")
    assert [row["id"] for row in page["messages"]] == [first["id"]]
    assert (retried["id"], retried["outcome"]) == (first["id"], "accepted_inbox_read")


def test_a_recipient_that_exits_before_reading_rejects_the_pending_send(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)

    async def exchange() -> SendReceipt:
        sending = asyncio.create_task(sender_client.send(recipient.address, "review", "disconnect", timeout_seconds=1))
        await asyncio.sleep(0.05)
        broker.unregister(recipient.address)
        return await asyncio.wait_for(sending, timeout=0.5)

    receipt = asyncio.run(exchange())
    assert receipt == _rejected(receipt["id"], recipient.address, "session_closed", queued=False)


def test_an_unread_message_evicted_from_a_full_inbox_is_rejected_dropped_unread(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    with patch.object(live_mailbox, "_MAX_INBOX_MESSAGES", 2):
        recipient = broker.register(room="ticket", harness="codex", label="recipient")
    client = MailboxClient(sender.socket_path, sender.token)

    async def exchange() -> SendReceipt:
        evicted = asyncio.create_task(client.send(recipient.address, "first", "first", timeout_seconds=5))
        await asyncio.sleep(0.05)
        await asyncio.gather(*(client.send(recipient.address, key, key, timeout_seconds=0.05) for key in ("b", "c")))
        return await asyncio.wait_for(evicted, timeout=1)

    receipt = asyncio.run(exchange())
    assert receipt == _rejected(receipt["id"], recipient.address, "dropped_unread", queued=False)


def test_a_sender_cannot_hold_more_than_its_share_of_waiting_sends(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    client = MailboxClient(sender.socket_path, sender.token)

    async def exchange() -> None:
        waiting = [
            asyncio.create_task(client.send(recipient.address, f"m{n}", f"k{n}", timeout_seconds=0.5))
            for n in range(live_mailbox.MAX_WAITING_SENDS)
        ]
        await asyncio.sleep(0.1)
        with pytest.raises(ToolError, match="waiting for acceptance"):
            await client.send(recipient.address, "one more", "k-over", timeout_seconds=0.5)
        await asyncio.gather(*waiting)

    asyncio.run(exchange())


def test_an_acknowledgement_outside_the_readers_own_inbox_accepts_nothing(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    intruder = MailboxClient(*_credentials(broker.register(room="ticket", harness="codex", label="intruder")))
    client = MailboxClient(sender.socket_path, sender.token)

    async def exchange() -> SendReceipt:
        sending = asyncio.create_task(client.send(recipient.address, "private", "p-1", timeout_seconds=0.3))
        await asyncio.sleep(0.05)
        await intruder._request("ack", ids=[1, 2, 3])
        return await sending

    receipt = asyncio.run(exchange())
    assert (receipt["outcome"], receipt["code"]) == ("rejected", "not_accepted_in_time")


def _credentials(identity: MailboxIdentity) -> tuple[Path, str]:
    return identity.socket_path, identity.token


def test_a_send_to_a_busy_steerable_peer_is_pushed_into_its_current_turn(
    routed_broker: LiveMailboxBroker, registry: LiveSessionRegistry
) -> None:
    sender = routed_broker.register(room="ticket", harness="codex_app_server", label="reviewing task 4")
    recipient = routed_broker.register(room="ticket", harness="claude_sdk", label="coding task 7", task=_TASK.pk)
    session = BoundarySession()
    client = MailboxClient(sender.socket_path, sender.token)

    with OwnerLoop(registry, _TASK, session) as owner:

        async def exchange() -> SendReceipt:
            sending = asyncio.create_task(client.send(recipient.address, "the schema moved", "s-1"))
            await asyncio.to_thread(_until, lambda: session.pending == 1)
            owner.call_soon(session.open_boundary)
            return await sending

        receipt = asyncio.run(exchange())

    assert receipt == {
        "id": receipt["id"],
        "to": recipient.address,
        "outcome": "accepted_current_turn",
        "code": None,
        "queued_in_inbox": False,
    }
    [delivered] = session.delivered
    header = f"[TeaTree peer message id={receipt['id']}] From another TeaTree agent: reviewing task 4"
    assert delivered.startswith(header)
    assert "the schema moved" in delivered


def test_an_inbox_read_accepts_first_and_withdraws_the_pending_push(
    routed_broker: LiveMailboxBroker, registry: LiveSessionRegistry
) -> None:
    sender = routed_broker.register(room="ticket", harness="claude_sdk", label="planner")
    recipient = routed_broker.register(room="ticket", harness="claude_sdk", label="coder", task=_TASK.pk)
    session = BoundarySession()
    client = MailboxClient(sender.socket_path, sender.token)
    reader = MailboxClient(recipient.socket_path, recipient.token)

    with OwnerLoop(registry, _TASK, session):

        async def exchange() -> SendReceipt:
            sending = asyncio.create_task(client.send(recipient.address, "read me", "r-1"))
            await asyncio.to_thread(_until, lambda: session.pending == 1)
            await reader.inbox()
            return await sending

        receipt = asyncio.run(exchange())
        _until(lambda: session.withdrawn == 1)

    assert (receipt["outcome"], receipt["code"]) == ("accepted_inbox_read", None)
    assert session.delivered == []


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

    async def exchange() -> SendReceipt:
        sending = asyncio.create_task(client.send(recipient.address, "ping", "once"))
        await MailboxClient(recipient.socket_path, recipient.token).wait(timeout_seconds=2)
        return await sending

    first = asyncio.run(exchange())
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
        asyncio.run(broker._send(captured, {"to": recipient.address, "text": "late", "key": "late"}))


def test_runner_binding_exposes_only_its_own_mcp_identity(broker: LiveMailboxBroker) -> None:
    options = ClaudeAgentOptions(mcp_servers={"teatree": {"type": "stdio", "command": "t3", "args": ["mcp", "serve"]}})
    with bound_task_mailbox(options, _TASK, broker=broker) as identity:
        assert identity is not None
        assert broker._identity(broker._by_address[identity.address]) == {
            "address": identity.address,
            "harness": "claude_sdk",
            "label": "coding task 7",
        }
        assert (broker._by_address[identity.address].room, broker._by_address[identity.address].task) == ("3", 7)
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


def test_every_mailbox_tool_tells_the_agent_what_it_does(broker: LiveMailboxBroker) -> None:
    identity = broker.register(room="ticket", harness="claude_sdk", label="coder")
    with patch.dict("os.environ", identity.mcp_env()):
        tools = {tool.name: tool.description or "" for tool in asyncio.run(build_server().list_tools())}

    mailbox = {name: text for name, text in tools.items() if name.startswith("agent_mailbox_")}
    assert len(mailbox) == 5
    assert all(mailbox.values())
    assert "accepted_current_turn" in mailbox["agent_mailbox_send"]
    assert "same key" in mailbox["agent_mailbox_send"]
    assert all("acknowledges" in mailbox[name] for name in ("agent_mailbox_inbox", "agent_mailbox_wait"))


def test_runner_binding_keeps_the_launch_flags(broker: LiveMailboxBroker) -> None:
    launch = ["mcp", "serve", "--read-only", "--allow-write", "review_request_post"]
    options = ClaudeAgentOptions(mcp_servers={"teatree": {"type": "stdio", "command": "t3", "args": launch}})
    with bound_task_mailbox(options, _TASK, broker=broker) as identity:
        assert identity is not None
        assert isinstance(options.mcp_servers, dict)
        assert cast("McpStdioServerConfig", options.mcp_servers["teatree"])["args"] == launch


def test_a_broker_that_cannot_register_leaves_the_dispatch_unbound(caplog: pytest.LogCaptureFixture) -> None:
    """The mailbox is a convenience of the run; a broken broker must not fail every dispatch."""

    class _BrokenBroker(LiveMailboxBroker):
        def register(self, *, room: str, harness: str, label: str, task: int | None = None) -> NoReturn:
            msg = "socket dir unwritable"
            raise OSError(msg)

    servers = {"teatree": {"type": "stdio", "command": "t3", "args": ["mcp", "serve"]}}
    options = ClaudeAgentOptions(mcp_servers=servers)
    with bound_task_mailbox(options, _TASK, broker=_BrokenBroker()) as identity:
        assert identity is None
        assert options.mcp_servers == servers
    assert "mailbox" in caplog.text


def test_interleaved_room_mail_does_not_report_false_truncation(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    first = broker.register(room="ticket", harness="codex", label="first")
    second = broker.register(room="ticket", harness="codex", label="second")
    client = MailboxClient(sender.socket_path, sender.token)

    async def exchange() -> InboxPage:
        one = asyncio.create_task(client.send(first.address, "one", "one"))
        two = asyncio.create_task(client.send(second.address, "two", "two"))
        await MailboxClient(first.socket_path, first.token).wait(timeout_seconds=2)
        inbox = await MailboxClient(second.socket_path, second.token).wait(timeout_seconds=2)
        await asyncio.gather(one, two)
        return inbox

    inbox = asyncio.run(exchange())

    assert inbox["messages"][0]["text"] == "two"
    assert inbox["truncated"] is False


def test_inbox_can_return_a_page_of_large_but_valid_messages(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)
    body = "x" * 16_000

    for index in range(5):
        invoke(sender_client, "send", to=recipient.address, text=body, key=f"large-{index}", timeout_seconds=0.01)

    inbox = invoke(recipient_client, "inbox")
    assert len(inbox["messages"]) == 5
    assert all(message["text"] == body for message in inbox["messages"])


def test_escaped_body_within_byte_limit_is_delivered(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)
    body = "\x00" * 16_384

    async def exchange() -> InboxPage:
        sending = asyncio.create_task(sender_client.send(recipient.address, body, "escaped"))
        page = await recipient_client.wait(timeout_seconds=2)
        await sending
        return page

    assert asyncio.run(exchange())["messages"][0]["text"] == body


def test_escaped_mailbox_pages_stay_within_transport_limit(broker: LiveMailboxBroker) -> None:
    sender = broker.register(room="ticket", harness="claude", label="sender")
    recipient = broker.register(room="ticket", harness="codex", label="recipient")
    sender_client = MailboxClient(sender.socket_path, sender.token)
    recipient_client = MailboxClient(recipient.socket_path, recipient.token)
    body = "\x00" * 16_384

    async def exchange() -> list[str]:
        for index in range(100):
            await sender_client.send(recipient.address, body, f"escaped-{index}", timeout_seconds=0.01)
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
            sent, inbox = await asyncio.gather(
                claude_session.call_tool(
                    "agent_mailbox_send",
                    {"to": codex.address, "text": "please review", "key": "stdio-once"},
                ),
                codex_session.call_tool("agent_mailbox_wait", {"after_id": 0, "timeout_seconds": 2}),
            )
            return cast("dict[str, Any]", structured(sent)), cast("dict[str, Any]", structured(inbox))

    sent, inbox = asyncio.run(exchange())
    assert sent["to"] == codex.address
    assert inbox["messages"][0]["text"] == "please review"


def test_the_published_socket_is_private_and_close_removes_only_it() -> None:
    live = Path(tempfile.mkdtemp(prefix="t3m-", dir="/tmp")) / "live"
    neighbour = live / "w-other.sock"
    with LiveMailboxBroker(runtime_dir=live) as broker:
        neighbour.touch()
        published = broker.socket_path

        assert published.parent == live
        assert stat.S_IMODE(live.stat().st_mode) == 0o700
        assert stat.S_IMODE(published.stat().st_mode) == 0o600
        assert published.suffix == ".sock"

    assert not published.exists()
    assert neighbour.exists()
    assert live.is_dir()


def test_two_brokers_share_one_live_dir_without_colliding() -> None:
    live = Path(tempfile.mkdtemp(prefix="t3m-", dir="/tmp")) / "live"
    with LiveMailboxBroker(runtime_dir=live) as first, LiveMailboxBroker(runtime_dir=live) as second:
        assert first.socket_path != second.socket_path
        assert {path.name for path in live.glob("*.sock")} == {first.socket_path.name, second.socket_path.name}


def test_an_overlong_live_dir_fails_loudly_instead_of_moving_the_socket() -> None:
    root = Path(tempfile.mkdtemp(prefix="t3m-", dir="/tmp"))
    broker = LiveMailboxBroker(runtime_dir=root / ("d" * 120))

    with pytest.raises(RuntimeError, match="socket path"):
        broker.start()

    assert not list(root.rglob("*.sock"))
    with pytest.raises(RuntimeError, match="has not started"):
        _ = broker.socket_path


def test_the_shared_broker_publishes_under_the_live_dir() -> None:
    broker = shared_broker()

    assert broker.socket_path.parent == live_mailbox.live_dir()
