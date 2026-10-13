import asyncio
import os
from pathlib import Path

import pytest

from teatree.agents.live_federation import (
    FederationRefusedError,
    FederationUnavailableError,
    proof,
    verified_sender,
    workers,
)
from teatree.agents.live_mailbox import LiveMailboxBroker


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


def test_discovery_refuses_an_unbounded_worker_directory(tmp_path: Path) -> None:
    for index in range(33):
        (tmp_path / f"w-{index}.sock").touch()

    with pytest.raises(FederationUnavailableError, match="discovery limit"):
        workers(tmp_path, tmp_path / "own.sock")
