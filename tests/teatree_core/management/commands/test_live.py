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
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.agents import live_mailbox
from teatree.agents.live_client import LiveClient, LiveDeliveryUnknownError, LiveOfflineError, LiveRefusedError
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.core.models import Task, TaskAttempt
from tests.factories import SessionFactory, TicketFactory

_REPO = Path(__file__).resolve().parents[4]
_HELPER = Path(__file__).with_name("live_worker_helper.py")
_PYDANTIC_HELPER = Path(__file__).with_name("live_pydantic_worker_helper.py")


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


@pytest.fixture
def pydantic_worker(control_dir: Path) -> Iterator[subprocess.Popen[str]]:
    with subprocess.Popen(
        [sys.executable, str(_PYDANTIC_HELPER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=_env(control_dir),
        start_new_session=True,
    ) as helper:
        assert helper.stdout is not None
        assert helper.stdout.readline().strip() == "ready"
        try:
            yield helper
        finally:
            if helper.stdin is not None and not helper.stdin.closed:
                with suppress(BrokenPipeError):
                    helper.stdin.write("\n\n\n")
                    helper.stdin.flush()


def test_live_list_and_steer_reach_a_pydantic_session_at_its_next_request(
    control_dir: Path, pydantic_worker: subprocess.Popen[str]
) -> None:
    cli = _Cli(control_dir)
    listed = cli("list", "--json")
    assert listed.returncode == 0, listed.stderr
    [row] = [row for row in json.loads(listed.stdout) if row["task"] == 105]
    assert (row["harness"], row["steerable"]) == ("pydantic_ai", True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        steering = pool.submit(cli, "steer", "105", "--text", "peer nonce", "--json")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            inspected = cli("inspect", "105", "--json")
            if inspected.returncode == 0 and json.loads(inspected.stdout)["pending_inputs"] == 1:
                break
        else:
            message = "input did not reach the pydantic session"
            raise AssertionError(message)
        assert not steering.done()
        assert pydantic_worker.stdin is not None
        pydantic_worker.stdin.write("go\n")
        pydantic_worker.stdin.flush()
        accepted = steering.result(timeout=15)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["outcome"] == "accepted_current_turn"
    request_log = control_dir / "requests.json"
    deadline = time.monotonic() + 10
    while not request_log.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    requests = json.loads(request_log.read_text())
    assert "peer nonce" not in requests[0]
    assert requests[1].count("peer nonce") == 1


def test_pydantic_steer_exit_codes_follow_turn_and_session_end(
    control_dir: Path, pydantic_worker: subprocess.Popen[str]
) -> None:
    cli = _Cli(control_dir)
    assert pydantic_worker.stdin is not None
    pydantic_worker.stdin.write("go\n")
    pydantic_worker.stdin.flush()
    ended = control_dir / "turn-ended"
    deadline = time.monotonic() + 10
    while not ended.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ended.exists()

    refused = cli("steer", "105", "--text", "late", "--json")
    assert refused.returncode == 3
    assert (json.loads(refused.stdout)["outcome"], json.loads(refused.stdout)["code"]) == ("rejected", "turn_ended")

    pydantic_worker.stdin.write("close\n")
    pydantic_worker.stdin.flush()
    closed = control_dir / "session-closed"
    deadline = time.monotonic() + 10
    while not closed.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert closed.exists()

    after_close = cli("steer", "105", "--text", "later", "--json")
    missing = cli("steer", "999", "--text", "x")
    assert after_close.returncode == 3
    assert json.loads(after_close.stdout)["code"] == "session_closed"
    assert missing.returncode == 5


def test_list_with_no_worker_socket_is_offline(control_dir: Path) -> None:
    completed = _Cli(control_dir)("list", "--json")

    assert completed.returncode == 5
    assert "offline" in completed.stderr


def test_follow_up_to_an_offline_task_names_the_durable_route(control_dir: Path) -> None:
    completed = _Cli(control_dir)("follow-up", "999", "--text", "continue", "--command-id", "next")

    assert completed.returncode == 5
    assert "task 999 in the task list" in completed.stderr
    assert "tasks create" in completed.stderr


def test_a_worker_restart_leaves_old_follow_ups_offline_and_replays_nothing(
    control_dir: Path, worker: subprocess.Popen[str]
) -> None:
    cli = _Cli(control_dir)
    client = LiveClient(live_dir=control_dir / "live")
    with pytest.raises(LiveDeliveryUnknownError):
        client.follow_up(107, "start", command_id="lost", wait=1)
    worker.wait(timeout=10)
    with pytest.raises(LiveOfflineError):
        client.follow_up(107, "start", command_id="lost")

    new_env = {**_env(control_dir), "T3_HELPER_TASK_BASE": "200"}
    with subprocess.Popen(
        [sys.executable, str(_HELPER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=new_env,
        start_new_session=True,
    ) as restarted:
        assert restarted.stdout is not None
        assert restarted.stdout.readline().strip() == "ready"
        with pytest.raises(LiveOfflineError):
            client.follow_up(107, "start", command_id="lost")
        calls_before = (control_dir / "follow-up-calls.log").read_text(encoding="utf-8").splitlines()
        explicit = cli("follow-up", "206", "--text", "new", "--command-id", "new")
        calls_after = (control_dir / "follow-up-calls.log").read_text(encoding="utf-8").splitlines()

    assert len(calls_before) == 1
    assert explicit.returncode == 0
    assert len(calls_after) == 2


@pytest.mark.usefixtures("worker")
def test_list_inspect_and_steer_a_live_session(control_dir: Path) -> None:
    cli = _Cli(control_dir)

    listed = cli("list", "--json")
    inspected = cli("inspect", "101")
    steered = cli("steer", "101", "--text", "use docs/x.md", "--command-id", "c-1", "--json")

    assert listed.returncode == 0, listed.stderr
    rows = {row["task"]: row for row in json.loads(listed.stdout)}
    assert set(rows) == {101, 102, 103, 104, 105, 106, 107}
    assert (rows[101]["harness"], rows[101]["state"], rows[101]["steerable"]) == ("claude_sdk", "busy", True)
    assert rows[104]["steerable"] is False
    assert rows[105]["steerable"] is True
    assert inspected.returncode == 0, inspected.stderr
    assert "PASSIVE" in inspected.stderr
    assert "Bash" in inspected.stderr
    assert steered.returncode == 0, steered.stderr
    receipt = json.loads(steered.stdout)
    assert (receipt["outcome"], receipt["command_id"], receipt["mode"]) == ("accepted_current_turn", "c-1", "active")
    assert "ACTIVE" in steered.stderr


@pytest.mark.usefixtures("worker")
def test_steer_with_an_old_attempt_exits_3_stale_command(control_dir: Path) -> None:
    cli = _Cli(control_dir)
    listed = json.loads(cli("list", "--json").stdout)
    current = next(row for row in listed if row["task"] == 101)
    stale = cli(
        "steer", "101", "--text", "old", "--attempt", "old-attempt", "--generation", current["generation"], "--json"
    )
    accepted = cli("steer", "101", "--text", "current", "--json")
    assert stale.returncode == 3
    assert (json.loads(stale.stdout)["outcome"], json.loads(stale.stdout)["code"]) == ("rejected", "stale_command")
    assert accepted.returncode == 0


@pytest.mark.usefixtures("worker")
def test_steer_with_an_old_generation_exits_3_stale_command(control_dir: Path) -> None:
    cli = _Cli(control_dir)
    listed = json.loads(cli("list", "--json").stdout)
    current = next(row for row in listed if row["task"] == 101)
    stale = cli(
        "steer", "101", "--text", "old", "--attempt", current["attempt"], "--generation", "old-generation", "--json"
    )

    assert stale.returncode == 3
    assert (json.loads(stale.stdout)["outcome"], json.loads(stale.stdout)["code"]) == ("rejected", "stale_command")


@pytest.mark.usefixtures("worker")
def test_live_steer_without_a_fence_is_refused(control_dir: Path) -> None:
    worker = next((control_dir / "live").glob("*.sock"))
    with pytest.raises(LiveRefusedError, match="needs an attempt and generation fence"):
        LiveClient._call(
            worker,
            {"method": "live.steer", "task": 101, "text": "x", "command_id": "unfenced", "wait_seconds": 1},
            timeout=2,
        )


@pytest.mark.usefixtures("worker")
def test_live_steer_reaches_a_codex_session_through_the_public_cli(control_dir: Path) -> None:
    cli = _Cli(control_dir)
    listed = json.loads(cli("list", "--json").stdout)
    codex = next(row for row in listed if row["task"] == 105)
    receipt = cli("steer", "105", "--text", "nonce-5417", "--json")
    assert (codex["harness"], codex["steerable"]) == ("codex_app_server", True)
    assert codex["attempt"]
    assert receipt.returncode == 0
    assert json.loads(receipt.stdout)["outcome"] == "accepted_current_turn"


def test_live_pause_command_returns_a_typed_receipt() -> None:
    receipt = {
        "command_id": "pause-123",
        "task": 123,
        "outcome": "paused",
        "code": None,
        "mode": "active",
        "accepted_at": "now",
    }
    with patch.object(LiveClient, "pause", return_value=receipt) as pause:
        output = StringIO()
        call_command("live", "pause", "123", "--command-id", "pause-123", "--json", stdout=output)

    pause.assert_called_once()
    assert json.loads(output.getvalue()) == receipt


@pytest.mark.parametrize(
    ("task", "extra", "exit_code", "expected"),
    [
        ("102", (), 3, ("rejected", "turn_ended")),
        ("104", (), 3, ("rejected", "not_steerable")),
        ("999", (), 5, None),
        ("103", ("--command-id", "c-lost"), 4, ("unknown_delivery", None)),
    ],
)
@pytest.mark.usefixtures("worker")
def test_steer_exit_codes_follow_the_receipt(
    control_dir: Path, task: str, extra: tuple[str, ...], exit_code: int, expected: tuple[str, str | None] | None
) -> None:
    completed = _Cli(control_dir)("steer", task, "--text", "x", *extra, "--json")

    assert completed.returncode == exit_code
    if expected is None:
        assert task in completed.stderr
    else:
        receipt = json.loads(completed.stdout)
        assert (receipt["outcome"], receipt["code"]) == expected
        if task == "103":
            assert "c-lost" in completed.stderr
            assert "unknown_delivery" in completed.stderr


@pytest.mark.parametrize("code", ["session_closed", "unknown_session"])
def test_follow_up_closed_receipts_use_the_offline_exit(code: str) -> None:
    receipt = {
        "command_id": "closed",
        "task": 123,
        "outcome": "rejected",
        "code": code,
        "mode": "active",
        "accepted_at": None,
    }
    stderr = StringIO()
    with (
        patch.object(LiveClient, "follow_up", return_value=receipt),
        patch("teatree.core.management.commands.live._task_list_status", return_value="running"),
        pytest.raises(SystemExit) as stopped,
    ):
        call_command("live", "follow-up", "123", "--text", "continue", stderr=stderr)

    assert stopped.value.code == 5
    assert "task 123 in the task list" in stderr.getvalue()


def test_follow_up_not_idle_receipt_gives_the_current_turn_hint() -> None:
    receipt = {
        "command_id": "busy",
        "task": 123,
        "outcome": "rejected",
        "code": "not_idle",
        "mode": "active",
        "accepted_at": None,
    }
    stderr = StringIO()
    with patch.object(LiveClient, "follow_up", return_value=receipt), pytest.raises(SystemExit) as stopped:
        call_command("live", "follow-up", "123", "--text", "continue", stderr=stderr)

    assert stopped.value.code == 3
    assert "use live steer for its current turn" in stderr.getvalue()


def test_a_legacy_worker_without_fence_fields_is_offline() -> None:
    legacy = {"task": 123, "worker": "old-worker"}
    with (
        patch.object(LiveClient, "sessions", return_value=[legacy]),
        pytest.raises(LiveOfflineError, match="worker old-worker"),
    ):
        LiveClient().follow_up(123, "continue", command_id="new")


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

    def test_follow_up_cannot_wake_a_parked_task_or_change_its_attempts(self) -> None:
        parked_until = datetime.now(UTC) + timedelta(hours=1)
        self.task.status = Task.Status.PENDING
        self.task.not_before = parked_until
        self.task.save(update_fields=["status", "not_before"])
        before = TaskAttempt.objects.filter(task=self.task).count()
        err = StringIO()

        with pytest.raises(SystemExit) as exit_info:
            call_command("live", "follow-up", str(self.task.pk), "--text", "continue", stderr=err)

        self.task.refresh_from_db()
        assert exit_info.value.code == 5
        assert "pending" in err.getvalue()
        assert self.task.status == Task.Status.PENDING
        assert self.task.not_before == parked_until
        assert TaskAttempt.objects.filter(task=self.task).count() == before
