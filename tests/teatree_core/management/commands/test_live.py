"""``t3 <overlay> live`` reaches the sessions a worker on this host is running.

The CLI runs as its own process (``python -m teatree live …``) against a helper worker process
that serves the real broker and registry; neither descends from the other, exactly as an
operator's ``docker exec`` session sits beside the deployed worker.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.agents import live_mailbox
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.core.models import Task
from tests.factories import SessionFactory, TicketFactory

_REPO = Path(__file__).resolve().parents[4]
_HELPER = Path(__file__).with_name("live_worker_helper.py")


def _env(control_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "T3_CONTROL_DB_DIR": str(control_dir),
        "DJANGO_SETTINGS_MODULE": "tests.django_settings",
        "PYTHONPATH": os.pathsep.join([str(_REPO), str(_REPO / "src"), str(_REPO / "tests")]),
    }


@dataclass
class _Cli:
    control_dir: Path

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "teatree", "live", *argv],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_env(self.control_dir),
        )


@pytest.fixture
def control_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="t3c-", dir="/tmp"))


@pytest.fixture
def worker(control_dir: Path) -> Iterator[subprocess.Popen[str]]:
    with subprocess.Popen(
        [sys.executable, str(_HELPER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=_env(control_dir),
        start_new_session=True,
    ) as helper:
        assert helper.stdout is not None
        assert helper.stdout.readline().strip() == "ready"
        yield helper


def test_list_with_no_worker_socket_is_offline(control_dir: Path) -> None:
    completed = _Cli(control_dir)("list", "--json")

    assert completed.returncode == 5
    assert "offline" in completed.stderr


@pytest.mark.usefixtures("worker")
def test_list_inspect_and_steer_a_live_session(control_dir: Path) -> None:
    cli = _Cli(control_dir)

    listed = cli("list", "--json")
    inspected = cli("inspect", "101")
    steered = cli("steer", "101", "--text", "use docs/x.md", "--command-id", "c-1", "--json")

    assert listed.returncode == 0, listed.stderr
    rows = {row["task"]: row for row in json.loads(listed.stdout)}
    assert set(rows) == {101, 102, 103, 104}
    assert (rows[101]["harness"], rows[101]["state"], rows[101]["steerable"]) == ("claude_sdk", "busy", True)
    assert rows[104]["steerable"] is False
    assert inspected.returncode == 0, inspected.stderr
    assert "PASSIVE" in inspected.stderr
    assert "Bash" in inspected.stderr
    assert steered.returncode == 0, steered.stderr
    receipt = json.loads(steered.stdout)
    assert (receipt["outcome"], receipt["command_id"], receipt["mode"]) == ("accepted_current_turn", "c-1", "active")
    assert "ACTIVE" in steered.stderr


@pytest.mark.usefixtures("worker")
def test_steer_exit_codes_follow_the_receipt(control_dir: Path) -> None:
    cli = _Cli(control_dir)

    rejected = cli("steer", "102", "--text", "x", "--json")
    not_steerable = cli("steer", "104", "--text", "x", "--json")
    not_live = cli("steer", "999", "--text", "x")
    lost = cli("steer", "103", "--text", "x", "--command-id", "c-lost")

    assert rejected.returncode == 3
    assert (json.loads(rejected.stdout)["outcome"], json.loads(rejected.stdout)["code"]) == ("rejected", "turn_ended")
    assert not_steerable.returncode == 3
    assert json.loads(not_steerable.stdout)["code"] == "not_steerable"
    assert not_live.returncode == 5
    assert "999" in not_live.stderr
    assert lost.returncode == 4
    assert "unknown_delivery" in lost.stderr
    assert "c-lost" in lost.stderr


@pytest.mark.usefixtures("worker")
def test_a_steer_the_worker_refuses_as_malformed_is_rejected_invalid_request(control_dir: Path) -> None:
    refused = _Cli(control_dir)("steer", "101", "--text", "x", "--wait", "901", "--json")

    assert refused.returncode == 3
    assert (json.loads(refused.stdout)["outcome"], json.loads(refused.stdout)["code"]) == (
        "rejected",
        "invalid_request",
    )
    assert "wait must be within" in refused.stderr


@pytest.fixture
def worker_parenting_the_cli(control_dir: Path) -> Iterator[LiveMailboxBroker]:
    with LiveMailboxBroker(runtime_dir=control_dir / "live") as broker:
        yield broker


@pytest.mark.usefixtures("worker_parenting_the_cli")
def test_an_agent_of_the_worker_is_refused_every_verb(control_dir: Path) -> None:
    cli = _Cli(control_dir)

    listed = cli("list")
    inspected = cli("inspect", "101")
    steered = cli("steer", "101", "--text", "x", "--json")

    assert (listed.returncode, inspected.returncode, steered.returncode) == (3, 3, 3)
    assert "rejected (permission_denied)" in listed.stderr
    assert "rejected (permission_denied)" in inspected.stderr
    assert json.loads(steered.stdout)["code"] == "permission_denied"


def test_a_socket_nobody_listens_on_is_removed_and_others_are_kept(control_dir: Path) -> None:
    live = control_dir / "live"
    live.mkdir(mode=0o700)
    stale = live / "w-stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()
    unreadable = live / "w-locked.sock"
    locked = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    locked.bind(str(unreadable))
    locked.listen()
    unreadable.chmod(0o000)

    completed = _Cli(control_dir)("list")

    locked.close()
    assert completed.returncode == 5
    assert not stale.exists()
    assert unreadable.exists()
    assert "w-locked" in completed.stderr


class OfflineNamesTheDurableRouteTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        empty = Path(tempfile.mkdtemp(prefix="t3e-", dir="/tmp"))
        patcher = patch.object(live_mailbox, "live_dir", return_value=empty)
        patcher.start()
        self.addCleanup(patcher.stop)
        ticket = TicketFactory()
        self.task = Task.objects.create(
            ticket=ticket, session=SessionFactory(ticket=ticket), phase="coding", status=Task.Status.COMPLETED
        )

    def test_inspect_of_a_task_no_worker_runs_names_its_task_list_status(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            call_command("live", "inspect", str(self.task.pk))

        assert exit_info.value.code == 5

    def test_the_offline_message_carries_status_and_route(self) -> None:
        err = StringIO()
        with pytest.raises(SystemExit):
            call_command("live", "steer", str(self.task.pk), "--text", "x", stderr=err)

        message = err.getvalue()
        assert f"task {self.task.pk}" in message
        assert "completed" in message
        # An owner question is answered in its Slack thread; `questions answer` refuses it.
        assert "Slack thread" in message
        assert "questions answer" not in message
