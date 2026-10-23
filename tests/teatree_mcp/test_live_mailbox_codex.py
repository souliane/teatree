"""Codex mailbox delivery through the App Server and worker-owned broker."""

import asyncio
import json
import sys
import tempfile
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.codex_app_server import CodexAppServerHarness, CodexAppServerSession
from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.codex_shared_app_server import SharedCodexAppServer
from teatree.agents.codex_turn_steering import CodexTurnSteering
from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness
from teatree.agents.live_binding import bound_task_mailbox
from teatree.agents.live_control import LiveFence, LiveTask, RejectCode, SteerRejectedError
from teatree.agents.live_delivery import InboxMessage, SendReceipt
from teatree.agents.live_mailbox import LiveMailboxBroker, PushOnlyTask
from teatree.agents.live_registry import LiveSessionRegistry
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import Task
from teatree.mcp.agent_mailbox import MailboxClient

_SERVER = """
import json
import sys
import time

log, mode = sys.argv[1:3]
for line in sys.stdin:
    request = json.loads(line)
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(request) + "\\n")
    method, ident = request.get("method"), request.get("id")
    if ident is None:
        continue
    if mode == "mailbox" and method is None:
        if ident == 900:
            peers = json.loads(request["result"]["contentItems"][0]["text"])
            recipient = next(peer for peer in peers if peer["label"] == "recipient")
            sys.stdout.write(json.dumps({"id": 901, "method": "item/tool/call", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "callId": "call-2", "namespace": None,
                "tool": "agent_mailbox_send", "arguments": {
                    "to": recipient["address"], "text": "nonce-from-codex", "key": "codex-1"
                }
            }}) + "\\n")
        elif ident == 901:
            receipt = json.loads(request["result"]["contentItems"][0]["text"])
            sys.stdout.write(json.dumps({"method": "item/completed", "params": {
                "threadId": "thread-1", "item": {"id": "item-2", "type": "agentMessage", "text": receipt["outcome"]}
            }}) + "\\n")
            sys.stdout.write(json.dumps({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}
            }}) + "\\n")
        sys.stdout.flush()
        continue
    if mode == "wrong-thread" and method is None and ident == 900:
        sys.stdout.write(json.dumps({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}
        }}) + "\\n")
        sys.stdout.flush()
        continue
    if method == "account/read":
        result = {"account": {"type": "chatgpt"}}
    elif method == "thread/start":
        result = {"thread": {"id": "thread-1"}}
    elif method == "mcpServerStatus/list":
        result = {"data": [], "nextCursor": None}
    elif method == "turn/start":
        time.sleep(0.5 if mode == "slow-start" else 0)
        result = {"turn": {"id": "turn-1"}}
    elif method == "turn/steer" and mode == "silent":
        continue
    elif method == "turn/steer":
        result = {"turnId": "turn-1"}
    else:
        result = {}
    sys.stdout.write(json.dumps({"id": ident, "result": result}) + "\\n")
    if method == "turn/steer":
        sys.stdout.write(json.dumps({"method": "item/completed", "params": {
            "threadId": "thread-1", "item": {"id": "item-1", "type": "agentMessage", "text": "nonce-5417"}
        }}) + "\\n")
        sys.stdout.write(json.dumps({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}
        }}) + "\\n")
    if method == "turn/start" and mode == "mailbox":
        sys.stdout.write(json.dumps({"id": 900, "method": "item/tool/call", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "callId": "call-1", "namespace": None,
            "tool": "agent_mailbox_peers", "arguments": {}
        }}) + "\\n")
    if method == "turn/start" and mode == "wrong-thread":
        sys.stdout.write(json.dumps({"id": 900, "method": "item/tool/call", "params": {
            "threadId": "another-thread", "turnId": "turn-1", "callId": "call-1", "namespace": None,
            "tool": "agent_mailbox_peers", "arguments": {}
        }}) + "\\n")
    sys.stdout.flush()
"""


class _Capture:
    tool_calls = 0
    context_tokens = None
    last_event_at = None
    open_tool = None
    result_message = None


class _UnsteerableSession:
    async def interrupt(self) -> None:
        return None


class _NoAuthCache:
    @asynccontextmanager
    async def session(self) -> AsyncGenerator[Path]:
        yield Path("/unused/auth.json")

    def persist(self) -> None:
        pass


def _fake_server(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    return script, tmp_path / "requests.jsonl"


async def _send_into_the_open_turn(client: MailboxClient, address: str) -> tuple[SendReceipt, list[SendReceipt]]:
    refused: list[SendReceipt] = []
    async with asyncio.timeout(5):
        while True:
            receipt = await client.send(address, "nonce-5417", f"message-{len(refused)}", timeout_seconds=1)
            if receipt["code"] != RejectCode.TURN_ENDED.value:
                return receipt, refused
            refused.append(receipt)
            await asyncio.sleep(0.01)


def _requests(log: Path, method: str) -> list[dict[str, Any]]:
    return [
        row
        for line in log.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line)).get("method") == method
    ]


def test_codex_dynamic_tools_send_a_peer_nonce_through_the_worker(tmp_path: Path) -> None:
    script, log = _fake_server(tmp_path)
    task = LiveTask(pk=7, ticket=3, phase="planning", harness="codex_app_server", model="codex")
    options = ClaudeAgentOptions(cwd=str(tmp_path), mcp_servers={})

    async def run() -> tuple[list[object], list[InboxMessage]]:
        with LiveMailboxBroker(runtime_dir=tmp_path) as broker, bound_task_mailbox(options, task, broker=broker):
            recipient = broker.register(room="3", harness="claude_sdk", label="recipient")
            recipient_client = MailboxClient(recipient.socket_path, recipient.token)
            session = CodexAppServerSession(
                CodexAppServerOptions.from_sdk_options(options),
                resume=None,
                code_home=tmp_path,
                command=(sys.executable, str(script), str(log), "mailbox"),
            )
            await session.start()
            try:
                await session.query("send the nonce")
                receiving = asyncio.create_task(recipient_client.wait(timeout_seconds=2))
                messages = [message async for message in session.receive_response()]
                return messages, (await receiving)["messages"]
            finally:
                await session.close()

    messages, received = asyncio.run(run())
    [start] = _requests(log, "thread/start")
    assert {tool["name"] for tool in start["params"]["dynamicTools"]} == {
        f"agent_mailbox_{name}" for name in ("self", "peers", "send", "inbox", "wait")
    }
    assert received[0]["text"] == "nonce-from-codex"
    assert any("accepted_inbox_read" in str(message) for message in messages)


def test_codex_dynamic_tool_rejects_another_thread(tmp_path: Path) -> None:
    script, log = _fake_server(tmp_path)
    options = ClaudeAgentOptions(cwd=str(tmp_path), mcp_servers={})
    task = LiveTask(pk=7, ticket=3, phase="planning", harness="codex_app_server", model="codex")

    async def run() -> None:
        with LiveMailboxBroker(runtime_dir=tmp_path) as broker, bound_task_mailbox(options, task, broker=broker):
            session = CodexAppServerSession(
                CodexAppServerOptions.from_sdk_options(options),
                resume=None,
                code_home=tmp_path,
                command=(sys.executable, str(script), str(log), "wrong-thread"),
            )
            await session.start()
            try:
                await session.query("read peers")
                _ = [message async for message in session.receive_response()]
            finally:
                await session.close()

    asyncio.run(run())
    replies = (json.loads(line) for line in log.read_text().splitlines())
    [request] = [row for row in replies if row.get("id") == 900 and "method" not in row]
    assert request["error"]["code"] == -32601


def _exchange(tmp_path: Path, *, mode: str = "accept") -> tuple[SendReceipt, list[dict[str, Any]], list[InboxMessage]]:
    registry = LiveSessionRegistry()
    script, log = _fake_server(tmp_path)
    options = ClaudeAgentOptions(cwd=str(tmp_path), mcp_servers={})
    task = LiveTask(pk=7, ticket=3, phase="coding", harness="codex_app_server", model="codex")

    async def run(runtime_dir: Path) -> tuple[SendReceipt, list[InboxMessage]]:
        session = CodexAppServerSession(
            CodexAppServerOptions.from_sdk_options(options),
            resume=None,
            code_home=tmp_path,
            command=(sys.executable, str(script), str(log), mode),
        )
        with (
            LiveMailboxBroker(runtime_dir=runtime_dir, registry=lambda: registry) as broker,
            bound_task_mailbox(options, task, broker=broker) as peer,
        ):
            assert peer is not None
            sender = broker.register(room="3", harness="claude_sdk", label="sender")
            client = MailboxClient(sender.socket_path, sender.token)
            await session.start()
            try:
                await session.query("wait for a peer")
                refusal = patch.object(
                    CodexTurnSteering, "steer", side_effect=SteerRejectedError(RejectCode.NOT_STEERABLE)
                )
                with refusal if mode == "no-steer" else nullcontext():
                    async with registry.attach(task, _Capture()) as controller:
                        controller.bind(session)
                        receipt = await client.send(peer.address, "nonce-5417", "message-1", timeout_seconds=0.2)
                        again = await client.send(peer.address, "nonce-5417", "message-1", timeout_seconds=0.2)
                        inbox = list(broker._by_token[peer.token].inbox)
            finally:
                await session.close()
        assert receipt == again
        return receipt, inbox

    with tempfile.TemporaryDirectory(prefix="t3l-", dir="/tmp") as runtime_dir:
        receipt, inbox = asyncio.run(run(Path(runtime_dir)))
    return receipt, _requests(log, "turn/steer"), inbox


def test_a_codex_task_without_mcp_has_a_worker_handled_inbox(tmp_path: Path) -> None:
    options = ClaudeAgentOptions(mcp_servers={})
    task = LiveTask(pk=7, ticket=3, phase="coding", harness="codex_app_server", model="codex")
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker, bound_task_mailbox(options, task, broker=broker) as peer:
        assert peer is not None
        sender = broker.register(room="3", harness="claude_sdk", label="sender")
        participants = broker.identities()
        assert any(row["address"] == peer.address and row["harness"] == "codex_app_server" for row in participants)
        assert options.mcp_servers == {}
        assert broker._by_token[peer.token].reads_inbox is True
        assert options.env is not None
        assert options.env["T3_AGENT_MAILBOX_TOKEN"] == peer.token
        assert sender.address != peer.address
    assert options.mcp_servers == {}


def test_claude_to_codex_send_is_admitted_through_broker_registry_and_codex_session(tmp_path: Path) -> None:
    receipt, steers, inbox = _exchange(tmp_path)
    assert (receipt["outcome"], receipt["queued_in_inbox"]) == ("accepted_current_turn", True)
    assert len(steers) == 1
    assert "nonce-5417" in steers[0]["params"]["input"][0]["text"]
    assert [row["text"] for row in inbox] == ["nonce-5417"]


@pytest.mark.parametrize(
    ("mode", "min_refusals"), [("accept", 0), ("slow-start", 1)], ids=["turn-opens-at-once", "turn-opens-late"]
)
def test_claude_to_shared_codex_runs_through_the_real_drive_and_broker(
    tmp_path: Path, mode: str, min_refusals: int
) -> None:
    registry = LiveSessionRegistry()
    script, log = _fake_server(tmp_path)
    manager = SharedCodexAppServer(
        code_home=tmp_path / "auth",
        cache=_NoAuthCache(),
        command=(sys.executable, str(script), str(log), mode),
        process_env={},
    )
    task = Task(pk=7, ticket_id=3, phase="coding")
    options = ClaudeAgentOptions(cwd=str(tmp_path), permission_mode="bypassPermissions", mcp_servers={})
    harness = GuardedHarness(
        CodexAppServerHarness(refusal=None, code_home=tmp_path / "auth"), CompactionGuard(), "codex_app_server"
    )
    runtime = HeartbeatRuntime(
        watchdog=LoopWatchdog(max_runtime_seconds=10, max_turns=0, max_cost_usd=0.0),
        heartbeat_interval=60,
        sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0),
        renew_lease=lambda _task: None,
        drain_reason=lambda: "",
    )

    async def run(broker: LiveMailboxBroker) -> tuple[SendReceipt, list[SendReceipt]]:
        sender = broker.register(room="3", harness="claude_sdk", label="sender")
        drive = asyncio.create_task(drive_with_heartbeat(task, "wait for a peer", options, harness, runtime=runtime))
        try:
            async with asyncio.timeout(5):
                while not any(row["harness"] == "codex_app_server" for row in broker.identities()):
                    await asyncio.to_thread(time.sleep, 0.01)
                while not (facts := registry.facts(7)) or not facts["steerable"]:
                    await asyncio.to_thread(time.sleep, 0.01)
            peer = next(row for row in broker.identities() if row["harness"] == "codex_app_server")
            receipt, refused = await _send_into_the_open_turn(
                MailboxClient(sender.socket_path, sender.token), peer["address"]
            )
            outcome = await asyncio.wait_for(drive, 5)
            assert outcome.result_message is not None
            assert "nonce-5417" in outcome.agent_text
            return receipt, refused
        finally:
            if not drive.done():
                drive.cancel()

    try:
        with (
            LiveMailboxBroker(runtime_dir=tmp_path, registry=lambda: registry) as broker,
            patch("teatree.agents.live_binding.shared_broker", return_value=broker),
            patch("teatree.agents.runner_heartbeat.shared_registry", return_value=registry),
            patch("teatree.agents.codex_shared_app_server.shared_codex_app_server", return_value=manager),
        ):
            receipt, refused = asyncio.run(run(broker))
    finally:
        manager.close()
    assert (receipt["outcome"], receipt["queued_in_inbox"]) == ("accepted_current_turn", True)
    assert len(refused) >= min_refusals
    steers = _requests(log, "turn/steer")
    assert len(steers) == 1
    assert "nonce-5417" in steers[0]["params"]["input"][0]["text"]


def test_unknown_delivery_to_a_codex_peer_stays_the_answer_to_a_same_key_retry(tmp_path: Path) -> None:
    receipt, steers, inbox = _exchange(tmp_path, mode="silent")
    assert (receipt["outcome"], receipt["queued_in_inbox"]) == ("unknown_delivery", True)
    assert len(steers) == 1
    assert [row["text"] for row in inbox] == ["nonce-5417"]


def test_the_delivery_scenario_fails_when_steering_or_routing_is_disabled(tmp_path: Path) -> None:
    receipt, steers, inbox = _exchange(tmp_path, mode="no-steer")
    assert (receipt["outcome"], receipt["code"], receipt["queued_in_inbox"]) == ("rejected", "not_steerable", True)
    assert steers == []
    assert [row["text"] for row in inbox] == ["nonce-5417"]

    options = ClaudeAgentOptions(mcp_servers={})
    unbound_claude = LiveTask(pk=8, ticket=3, phase="coding", harness="claude_sdk", model="test")
    with (
        LiveMailboxBroker(runtime_dir=tmp_path) as broker,
        bound_task_mailbox(options, unbound_claude, broker=broker) as peer,
    ):
        assert peer is None
        sender = broker.register(room="3", harness="claude_sdk", label="sender")
        assert asyncio.run(MailboxClient(sender.socket_path, sender.token).peers()) == []
        with pytest.raises(ValueError, match="recipient is not active"):
            broker.enqueue_from(broker.identities()[0], "missing-recipient", "nonce-5417")


def test_a_push_only_peer_never_claims_an_inbox_copy() -> None:
    task = LiveTask(pk=7, ticket=3, phase="coding", harness="codex_app_server", model="codex")
    with tempfile.TemporaryDirectory(prefix="t3p-", dir="/tmp") as runtime_dir:
        with LiveMailboxBroker(runtime_dir=Path(runtime_dir)) as broker:
            recipient = broker.register(
                room="3", harness="codex_app_server", label="recipient", task=PushOnlyTask(task)
            )
            sender = broker.register(room="3", harness="claude_sdk", label="sender")
            receipt = asyncio.run(
                MailboxClient(sender.socket_path, sender.token).send(
                    recipient.address, "nonce-5417", "message-1", timeout_seconds=0.1
                )
            )
            inbox = list(broker._by_token[recipient.token].inbox)
        assert receipt["queued_in_inbox"] is False
        assert inbox == []


def test_a_push_queued_for_an_old_participant_is_stale_in_the_new_attempt(tmp_path: Path) -> None:
    registry = LiveSessionRegistry()
    task = LiveTask(
        pk=7, ticket=3, phase="coding", harness="codex_app_server", model="codex", fence=LiveFence("new", "g")
    )
    with LiveMailboxBroker(runtime_dir=tmp_path, registry=lambda: registry) as broker:
        sender = broker.register(room="3", harness="claude_sdk", label="sender")
        old_peer = broker.register(
            room="3",
            harness="codex_app_server",
            label="old",
            task=PushOnlyTask(
                LiveTask(
                    pk=7,
                    ticket=3,
                    phase="coding",
                    harness="codex_app_server",
                    model="codex",
                    fence=LiveFence("old", "g"),
                )
            ),
        )

        async def send() -> SendReceipt:
            async with registry.attach(task, _Capture()) as controller:
                controller.bind(_UnsteerableSession())
                return await MailboxClient(sender.socket_path, sender.token).send(
                    old_peer.address, "nonce-5417", "old-message", timeout_seconds=0.2
                )

        receipt = asyncio.run(send())
    assert (receipt["outcome"], receipt["code"], receipt["queued_in_inbox"]) == ("rejected", "stale_command", False)
