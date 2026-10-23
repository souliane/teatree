"""Operator verbs on the worker socket are refused to the worker's own agent processes."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.agents.live_client import LiveClient
from teatree.agents.live_control import MAX_STEER_WAIT_SECONDS, LiveFence, LiveTask
from teatree.agents.live_ingress import (
    IngressRefusedError,
    OperatorIngress,
    PeerCredentials,
    PeerRole,
    classify_peer,
    peer_credentials,
)
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_registry import LiveSessionRegistry
from tests.teatree_agents._live_owner import OwnerLoop

_AGENT_CHILD = """
import asyncio, json, socket, sys
from pathlib import Path
from teatree.mcp.agent_mailbox import MailboxClient

socket_path, token = Path(sys.argv[1]), sys.stdin.readline().strip()
peers = asyncio.run(MailboxClient(socket_path, token).peers())
raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
raw.connect(str(socket_path))
raw.sendall(json.dumps({"method": "live.list"}).encode() + b"\\n")
answer = json.loads(raw.makefile().readline())
relay = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
relay.connect(str(socket_path))
relay.sendall(json.dumps({"method": "relay.peers"}).encode() + b"\\n")
relay_answer = json.loads(relay.makefile().readline())
print(json.dumps({"peers": peers, "live_list": answer, "relay_peers": relay_answer}))
"""


@pytest.fixture
def broker() -> Iterator[LiveMailboxBroker]:
    with LiveMailboxBroker(runtime_dir=Path(tempfile.mkdtemp(prefix="t3i-", dir="/tmp"))) as running:
        yield running


def test_an_agent_child_is_refused_operator_verbs_but_keeps_its_mailbox(broker: LiveMailboxBroker) -> None:
    agent = broker.register(room="ticket-1", harness="claude_sdk", label="coder")
    broker.register(room="ticket-1", harness="claude_sdk", label="reviewer")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}

    completed = subprocess.run(
        [sys.executable, "-c", _AGENT_CHILD, str(agent.socket_path)],
        input=agent.token,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=env,
    )

    answer = json.loads(completed.stdout)
    assert [peer["label"] for peer in answer["peers"]] == ["reviewer"]
    assert answer["live_list"]["code"] == "permission_denied"
    assert "result" not in answer["live_list"]
    assert answer["relay_peers"]["code"] == "permission_denied"


def _proc_tree(root: Path, parents: dict[int, int]) -> Path:
    for pid, parent in parents.items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "status").write_text(f"Name:\tx\nPPid:\t{parent}\nUid:\t1000\n", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("credentials", "expected"),
    [
        pytest.param(PeerCredentials(pid=7, uid=1000), PeerRole.OPERATOR, id="the-worker-itself"),
        pytest.param(PeerCredentials(pid=0, uid=1000), PeerRole.OPERATOR, id="another-pid-namespace"),
        pytest.param(PeerCredentials(pid=300, uid=1000), PeerRole.OPERATOR, id="exec-session-outside-the-worker"),
        pytest.param(PeerCredentials(pid=200, uid=1000), PeerRole.AGENT, id="grandchild-of-the-worker"),
        pytest.param(PeerCredentials(pid=200, uid=1001), PeerRole.REFUSED, id="another-uid"),
        pytest.param(PeerCredentials(pid=999, uid=1000), PeerRole.REFUSED, id="unreadable-proc"),
        pytest.param(None, PeerRole.REFUSED, id="no-peer-credentials"),
    ],
)
def test_peer_classification(tmp_path: Path, credentials: PeerCredentials | None, expected: PeerRole) -> None:
    proc = _proc_tree(tmp_path, {1: 0, 7: 1, 100: 7, 200: 100, 300: 0})

    assert classify_peer(credentials, worker_pid=7, own_uid=1000, proc_root=proc) is expected


def test_credentials_are_read_from_a_connected_unix_socket() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with left, right:
        assert peer_credentials(left) == PeerCredentials(pid=os.getpid(), uid=os.getuid())
    assert peer_credentials(None) is None


def _served_by_an_operator(method: str, request: dict[str, object]) -> object:
    registry = LiveSessionRegistry()
    ingress = OperatorIngress(lambda: registry, worker_pid=os.getpid())
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with left, right:
        return asyncio.run(ingress.serve(method, {"method": method, **request}, left))


_STEER = {
    "task": 7,
    "text": "use docs/x.md",
    "command_id": "c-1",
    "attempt": "attempt-1",
    "generation": "generation-1",
    "wait_seconds": 5,
}


@pytest.mark.parametrize(
    ("method", "request_fields", "refusal"),
    [
        pytest.param("live.inspect", {}, "positive integer task", id="no-task"),
        pytest.param("live.inspect", {"task": 0}, "positive integer task", id="task-zero"),
        pytest.param("live.inspect", {"task": True}, "positive integer task", id="task-bool"),
        pytest.param("live.inspect", {"task": "7"}, "positive integer task", id="task-string"),
        pytest.param("live.steer", {**_STEER, "text": ""}, "non-empty text", id="empty-text"),
        pytest.param("live.follow_up", {**_STEER, "text": ""}, "non-empty text", id="follow-up-empty-text"),
        pytest.param("live.steer", {**_STEER, "text": 5}, "non-empty text", id="text-not-a-string"),
        pytest.param("live.steer", {**_STEER, "command_id": ""}, "1-128 byte command_id", id="empty-command-id"),
        pytest.param("live.steer", {**_STEER, "command_id": "é" * 65}, "1-128 byte command_id", id="over-128-bytes"),
        pytest.param("live.steer", {**_STEER, "attempt": None}, "attempt and generation fence", id="no-attempt"),
        pytest.param("live.steer", {**_STEER, "generation": None}, "attempt and generation fence", id="no-generation"),
        pytest.param("live.steer", {**_STEER, "wait_seconds": 0}, "wait must be within", id="no-wait"),
        pytest.param("live.steer", {**_STEER, "wait_seconds": 900.5}, "wait must be within", id="wait-over-cap"),
        pytest.param("live.steer", {**_STEER, "wait_seconds": True}, "wait must be within", id="wait-bool"),
        pytest.param("live.steer", {**_STEER, "wait_seconds": "5"}, "wait must be within", id="wait-string"),
        pytest.param(
            "live.steer",
            {**_STEER, "generation": 3},
            "attempt and generation fence",
            id="fence-generation-type",
        ),
        pytest.param("live.kill", {"task": 7}, "Unknown live method", id="unknown-verb"),
    ],
)
def test_a_malformed_operator_request_is_refused_before_any_session_is_touched(
    method: str, request_fields: dict[str, object], refusal: str
) -> None:
    with pytest.raises(ValueError, match=refusal):
        _served_by_an_operator(method, request_fields)


def test_a_request_at_every_bound_reaches_the_registry() -> None:
    boundary = {**_STEER, "command_id": "c" * 128, "wait_seconds": MAX_STEER_WAIT_SECONDS}

    receipt = _served_by_an_operator("live.steer", boundary)

    assert receipt == {
        "command_id": "c" * 128,
        "task": 7,
        "outcome": "rejected",
        "code": "unknown_session",
        "mode": "active",
        "accepted_at": None,
    }


def test_a_valid_follow_up_reaches_the_registry() -> None:
    receipt = _served_by_an_operator("live.follow_up", _STEER)
    assert receipt == {
        "command_id": "c-1",
        "task": 7,
        "outcome": "rejected",
        "code": "unknown_session",
        "mode": "active",
        "accepted_at": None,
    }


def test_live_pause_reaches_the_registry_and_is_refused_to_an_agent_child() -> None:
    receipt = _served_by_an_operator("live.pause", _STEER)
    assert isinstance(receipt, dict)
    assert receipt["code"] == "unknown_session"

    ingress = OperatorIngress(LiveSessionRegistry, worker_pid=os.getpid())
    with (
        patch("teatree.agents.live_ingress.classify_peer", return_value=PeerRole.AGENT),
        pytest.raises(IngressRefusedError) as refused,
    ):
        asyncio.run(ingress.serve("live.pause", {"method": "live.pause", **_STEER}, None))
    assert refused.value.code.value == "permission_denied"


def test_a_legacy_worker_generation_is_a_valid_fence_value() -> None:
    request = {**_STEER, "attempt": "worker|session|time", "generation": ""}

    receipt = _served_by_an_operator("live.follow_up", request)

    assert isinstance(receipt, dict)
    assert receipt["code"] == "unknown_session"


def test_operator_socket_carries_the_claim_and_worker_generation_of_the_selected_session(tmp_path: Path) -> None:
    class Session:
        def __init__(self) -> None:
            self.delivered: list[str] = []

        async def steer(self, text: str, *, input_id: str, wait: float) -> None:
            self.delivered.append(text)

        async def interrupt(self) -> None:
            return None

    registry = LiveSessionRegistry()
    task = LiveTask(7, 3, "coding", "claude_sdk", "m", LiveFence("new", "worker-b"))
    session = Session()
    ingress = OperatorIngress(lambda: registry, worker_pid=os.getpid())
    with LiveMailboxBroker(runtime_dir=tmp_path, ingress=ingress), OwnerLoop(registry, task, session):
        client = LiveClient(live_dir=tmp_path)
        for old in (
            {"attempt": "old", "generation": "worker-b"},
            {"attempt": "new", "generation": "worker-a"},
        ):
            request = {"method": "live.steer", **_STEER, **old}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connected:
                connected.connect(str(next(iter(client.workers()))))
                connected.sendall(json.dumps(request).encode() + b"\n")
                stale = json.loads(connected.makefile("rb").readline())["result"]
            assert stale["code"] == "stale_command"
        current = client.steer(task.pk, "current", command_id="current", wait=1)

    assert current["outcome"] == "accepted_current_turn"
    assert len(session.delivered) == 1
