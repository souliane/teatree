import asyncio
import os
import socket
from pathlib import Path

import pytest

from teatree.agents.live_federation import FederationRefusedError, WorkerGoneError, call, proof, verified_sender
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.mcp.agent_mailbox import MailboxClient


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
