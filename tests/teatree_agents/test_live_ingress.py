"""Operator verbs on the worker socket are refused to the worker's own agent processes."""

import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from teatree.agents.live_ingress import PeerCredentials, PeerRole, classify_peer, peer_credentials
from teatree.agents.live_mailbox import LiveMailboxBroker

_AGENT_CHILD = """
import asyncio, json, socket, sys
from pathlib import Path
from teatree.mcp.agent_mailbox import MailboxClient

socket_path, token = Path(sys.argv[1]), sys.argv[2]
peers = asyncio.run(MailboxClient(socket_path, token).peers())
raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
raw.connect(str(socket_path))
raw.sendall(json.dumps({"method": "live.list"}).encode() + b"\\n")
answer = json.loads(raw.makefile().readline())
print(json.dumps({"peers": peers, "live_list": answer}))
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
        [sys.executable, "-c", _AGENT_CHILD, str(agent.socket_path), agent.token],
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
