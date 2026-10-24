import asyncio
import gc
import os
import socket
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_delivery import InboxPage, SendReceipt
from teatree.agents.live_federation import (
    FederationRefusedError,
    WorkerGoneError,
    _published,
    _worker_process,
    call,
    proof,
    verified_sender,
)
from teatree.agents.live_mailbox import LiveMailboxBroker, reset_shared_brokers
from teatree.agents.live_mailbox_client import InteractiveMailboxLease
from teatree.mcp.agent_mailbox import MailboxClient


def test_attended_peer_discovers_and_messages_an_agent_on_another_worker(tmp_path: Path) -> None:
    with (
        LiveMailboxBroker(runtime_dir=tmp_path),
        InteractiveMailboxLease.connect(tmp_path) as attended,
        LiveMailboxBroker(runtime_dir=tmp_path) as second,
    ):
        agent = second.register(room="ticket-2", harness="claude_sdk", label="coder")
        attended_client = MailboxClient(attended.socket_path, attended.token)
        agent_client = MailboxClient(agent.socket_path, agent.token)

        async def exchange() -> tuple[list[PeerIdentity], SendReceipt, InboxPage]:
            peers = await attended_client.peers()
            sending = asyncio.create_task(attended_client.send(agent.address, "nonce-b", "remote-1"))
            received = await agent_client.wait(timeout_seconds=2)
            return peers, await sending, received

        peers, receipt, received = asyncio.run(exchange())
        assert any(peer["address"] == agent.address for peer in peers)
        assert receipt["outcome"] == "accepted_inbox_read"
        assert received["messages"][0]["text"] == "nonce-b"


def test_peer_from_another_pid_namespace_requires_a_matching_source_broker_proof(tmp_path: Path) -> None:
    with (
        LiveMailboxBroker(runtime_dir=tmp_path) as source,
        LiveMailboxBroker(runtime_dir=tmp_path) as recipient,
    ):
        sender = source.register(room="ticket-1", harness="claude_sdk", label="planner")
        target = recipient.register(room="ticket-2", harness="codex_app_server", label="coder")
        request = {
            "action": "send",
            "source_socket": str(source.socket_path),
            "source_address": sender.address,
            "to": target.address,
            "text": "review this",
            "key": "one",
        }
        request["proof"] = proof(source._relay.secret, request)

        verified = asyncio.run(verified_sender(request, own_socket=recipient.socket_path, connected_pid=0))
        assert verified["address"] == sender.address

        request["text"] = "changed"
        with pytest.raises(FederationRefusedError, match="source could not be verified"):
            asyncio.run(verified_sender(request, own_socket=recipient.socket_path, connected_pid=0))


def test_a_valid_proof_relayed_by_a_process_other_than_the_source_worker_is_refused(tmp_path: Path) -> None:
    with (
        LiveMailboxBroker(runtime_dir=tmp_path) as source,
        LiveMailboxBroker(runtime_dir=tmp_path) as recipient,
    ):
        sender = source.register(room="ticket-1", harness="claude_sdk", label="planner")
        request = {
            "action": "peers",
            "source_socket": str(source.socket_path),
            "source_address": sender.address,
            "to": "",
            "text": "",
            "key": "",
        }
        request["proof"] = proof(source._relay.secret, request)

        verified = asyncio.run(verified_sender(request, own_socket=recipient.socket_path, connected_pid=os.getpid()))
        assert verified["address"] == sender.address
        with pytest.raises(FederationRefusedError, match="source could not be verified"):
            asyncio.run(verified_sender(request, own_socket=recipient.socket_path, connected_pid=os.getpid() + 1))


def test_a_source_socket_outside_the_live_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "live").mkdir()
    request = {"source_socket": str(tmp_path / "w-elsewhere.sock"), "source_address": "addr"}

    with pytest.raises(FederationRefusedError, match="outside the live directory"):
        asyncio.run(verified_sender(request, own_socket=tmp_path / "live" / "w-own.sock", connected_pid=0))


def test_a_broker_cannot_claim_its_own_socket_as_another_worker(tmp_path: Path) -> None:
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        sender = broker.register(room="first", harness="claude_sdk", label="sender")
        request = {
            "action": "peers",
            "source_socket": str(broker.socket_path),
            "source_address": sender.address,
            "to": "",
            "text": "",
            "key": "",
        }
        request["proof"] = proof(broker._relay.secret, request)

        with pytest.raises(FederationRefusedError, match="outside the live directory"):
            asyncio.run(verified_sender(request, own_socket=broker.socket_path, connected_pid=os.getpid()))


def test_relay_action_must_match_the_proved_method(tmp_path: Path) -> None:
    with (
        LiveMailboxBroker(runtime_dir=tmp_path) as source,
        LiveMailboxBroker(runtime_dir=tmp_path) as recipient,
    ):
        sender = source.register(room="first", harness="claude_sdk", label="sender")
        request = {
            "method": "relay.peers",
            "action": "send",
            "source_socket": str(source.socket_path),
            "source_address": sender.address,
            "to": "",
            "text": "",
            "key": "",
        }
        request["proof"] = proof(source._relay.secret, request)

        with pytest.raises(FederationRefusedError, match="action does not match"):
            asyncio.run(call(recipient.socket_path, request, wait=1))


def test_a_source_broker_rejects_a_proof_for_another_socket(tmp_path: Path) -> None:
    with (
        LiveMailboxBroker(runtime_dir=tmp_path) as source,
        LiveMailboxBroker(runtime_dir=tmp_path) as other,
    ):
        sender = source.register(room="first", harness="claude_sdk", label="sender")
        request = {
            "method": "relay.verify",
            "action": "peers",
            "source_socket": str(other.socket_path),
            "source_address": sender.address,
            "to": "",
            "text": "",
            "key": "",
        }
        request["proof"] = proof(source._relay.secret, request)

        with pytest.raises(FederationRefusedError, match="source could not be verified"):
            asyncio.run(call(source.socket_path, request, wait=1))


def test_a_verified_source_cannot_substitute_another_sender_address(tmp_path: Path) -> None:
    request = {"source_socket": str(tmp_path / "w-source.sock"), "source_address": "claimed"}
    identity = {"pid": 7, "sender": {"address": "other", "ticket": "first", "harness": "claude_sdk", "label": "peer"}}

    with (
        patch("teatree.agents.live_federation.call", new_callable=AsyncMock, return_value=identity),
        pytest.raises(FederationRefusedError, match="sender is not active"),
    ):
        asyncio.run(verified_sender(request, own_socket=tmp_path / "w-own.sock", connected_pid=7))


@pytest.mark.parametrize(
    "ticket_claim",
    [{"harness": "claude_sdk"}, {"harness": "interactive", "ticket": "first"}],
    ids=["agent_without_a_ticket", "attended_with_a_ticket"],
)
def test_a_sender_whose_ticket_does_not_match_its_harness_is_refused(
    tmp_path: Path, ticket_claim: dict[str, str]
) -> None:
    request = {"source_socket": str(tmp_path / "w-source.sock"), "source_address": "claimed"}
    identity = {"pid": 7, "sender": {"address": "claimed", "label": "peer", **ticket_claim}}

    with (
        patch("teatree.agents.live_federation.call", new_callable=AsyncMock, return_value=identity),
        pytest.raises(FederationRefusedError, match="sender is not active"),
    ):
        asyncio.run(verified_sender(request, own_socket=tmp_path / "w-own.sock", connected_pid=7))


def test_a_relay_key_cannot_be_reused_for_different_text(tmp_path: Path) -> None:
    with (
        LiveMailboxBroker(runtime_dir=tmp_path) as source,
        LiveMailboxBroker(runtime_dir=tmp_path) as recipient,
    ):
        sender = source.register(room="first", harness="claude_sdk", label="sender")
        target = recipient.register(room="second", harness="claude_sdk", label="target")
        request = {
            "method": "relay.send",
            "action": "send",
            "source_socket": str(source.socket_path),
            "source_address": sender.address,
            "to": target.address,
            "text": "first",
            "key": "same",
            "timeout_seconds": 0.01,
        }
        request["proof"] = proof(source._relay.secret, request)
        asyncio.run(call(recipient.socket_path, request, wait=1))
        request["text"] = "changed"
        request["proof"] = proof(source._relay.secret, request)

        with pytest.raises(FederationRefusedError, match="already used"):
            asyncio.run(call(recipient.socket_path, request, wait=1))


def test_discovery_removes_stale_sockets_before_enforcing_the_worker_limit(tmp_path: Path) -> None:
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        sender = broker.register(room="first", harness="claude_sdk", label="sender")
        peer = broker.register(room="second", harness="codex_app_server", label="peer")
        for index in range(1_025):
            path = tmp_path / f"w-junk{index}.sock"
            with socket.socket(socket.AF_UNIX) as abandoned:
                abandoned.bind(str(path))

        peers = asyncio.run(MailboxClient(sender.socket_path, sender.token).peers())
        assert [row["address"] for row in peers] == [peer.address]
        assert not list(tmp_path.glob("w-junk*.sock"))


def test_a_socket_its_worker_already_removed_reads_as_a_gone_worker(tmp_path: Path) -> None:
    with pytest.raises(WorkerGoneError, match="removed by its worker"):
        asyncio.run(call(tmp_path / "w-gone.sock", {"method": "relay.peers"}, wait=1))


def test_remote_process_must_be_running_the_worker_command() -> None:
    with patch.object(Path, "read_bytes", return_value=b"/bin/python\0/bin/t3\0worker\0"):
        assert _worker_process(7)
    with patch.object(Path, "read_bytes", return_value=b"/bin/python\0/bin/t3\0status\0"):
        assert not _worker_process(7)


def test_a_public_mode_socket_is_not_trusted_for_peer_discovery(tmp_path: Path) -> None:
    recipient = LiveMailboxBroker(runtime_dir=tmp_path)
    served = threading.Event()
    handle = recipient._handle

    async def handle_then_signal(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await handle(reader, writer)
        finally:
            served.set()

    with (
        LiveMailboxBroker(runtime_dir=tmp_path) as source,
        patch.object(recipient, "_handle", handle_then_signal),
        recipient,
    ):
        sender = source.register(room="first", harness="claude_sdk", label="sender")
        target = recipient.register(room="second", harness="claude_sdk", label="recipient")
        recipient.socket_path.chmod(0o666)

        peers = asyncio.run(MailboxClient(sender.socket_path, sender.token).peers())
        assert target.address not in {peer["address"] for peer in peers}
        # The source refuses before sending, so a shutdown can race the recipient's accept and leak its transport.
        assert served.wait(timeout=5)
    # Report a leaked transport in this test, not in whichever test the collector next runs in.
    gc.collect()


def test_reset_shared_brokers_forgets_published_socket_paths(tmp_path: Path) -> None:
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        assert broker.socket_path in _published
        reset_shared_brokers()
        assert broker.socket_path not in _published


def test_closed_peer_connection_gets_no_error_response(tmp_path: Path) -> None:
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker, socket.socket(socket.AF_UNIX) as client:
        client.settimeout(1)
        client.connect(str(broker.socket_path))
        client.shutdown(socket.SHUT_WR)
        assert client.recv(1024) == b""


def test_cancelled_client_read_closes_server_transport(tmp_path: Path) -> None:
    async def cancelled_read() -> bool:
        server_socket, client_socket = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=server_socket)
        try:
            broker = LiveMailboxBroker(runtime_dir=tmp_path)
            task = asyncio.create_task(broker._handle(reader, writer))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return writer.is_closing()
        finally:
            writer.close()
            await writer.wait_closed()
            client_socket.close()

    assert asyncio.run(cancelled_read())
