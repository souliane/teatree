"""manage.py worker — the singleton loop-timer worker command (#1796).

The command wires the flock singleton + SIGTERM/SIGINT handlers around the
:class:`teatree.loops.worker.LoopWorker`; a second invocation while one holds the
flock refuses with a non-zero exit. Collaborators are stubbed so the test never
spawns a real, forever-blocking worker or mutates the process's signal handlers.

Losing the race is also RECORDED (#3976): the supervisor restarts the refused worker
forever, so the count of consecutive identical refusals is the only thing that can tell
a deploy hand-over apart from a race this deployment can never win.
"""

import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import worker as worker_command
from teatree.core.management.commands.worker import STARVED_EXIT
from teatree.core.models import WorkerGeneration
from teatree.utils import singleton as singleton_mod
from teatree.utils.singleton import WORKER_SINGLETON, AlreadyRunningError, ExecutionContext, flock_is_held
from teatree.utils.singleton_refusals import ESCALATION_THRESHOLD, read_streak, record_refusal

_OUTSIDE_HOLDER = ExecutionContext(pid_namespace="pid:[1]", hostname="box", role="")


@pytest.fixture(autouse=True)
def _singleton_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point both worker locks and the refusal ledger at a per-test dir.

    The tests that let the command really acquire the singleton would otherwise all
    race on one shared lock file, so two xdist workers refuse each other.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("teatree.utils.singleton.DATA_DIR", data)
    monkeypatch.setattr(singleton_mod, "CANONICAL_DB", data / "db.sqlite3")
    monkeypatch.setattr("teatree.utils.singleton_refusals.DATA_DIR", data)
    return data


def _refuse_from_outside(_name: str) -> None:
    raise AlreadyRunningError(WORKER_SINGLETON, 4321, Path("worker.pid"), holder_context=_OUTSIDE_HOLDER)


def test_runs_the_worker_under_the_singleton_and_installs_signals() -> None:
    with (
        patch("teatree.loops.worker.LoopWorker") as worker_cls,
        patch("teatree.core.management.commands.worker.signal.signal") as signal_signal,
    ):
        call_command("worker")
    worker_cls.return_value.run.assert_called_once_with()
    assert signal_signal.call_count == 2  # SIGTERM + SIGINT


def test_worker_holds_the_isolated_control_database_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "data" / "db.sqlite3.worker.pid"

    def check_running() -> None:
        assert flock_is_held(WORKER_SINGLETON, pid_path=lock_path)

    with (
        patch("teatree.loops.worker.LoopWorker") as worker_cls,
        patch("teatree.core.management.commands.worker.signal.signal"),
    ):
        worker_cls.return_value.run.side_effect = check_running
        call_command("worker")
    assert not flock_is_held(WORKER_SINGLETON, pid_path=lock_path)


def test_worker_holds_the_control_database_lock_while_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "control" / "db.sqlite3"
    monkeypatch.setattr(singleton_mod, "CANONICAL_DB", database)
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "config.sqlite3"))
    db_lock = database.with_name(f"{database.name}.worker.pid")

    def second_worker_exit() -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(tmp_path / "other-home")
        env["T3_DATA_DIR"] = str(tmp_path / "other-worker-state")
        env["T3_CONTROL_DB_DIR"] = str(database.parent)
        env.pop("XDG_DATA_HOME", None)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from teatree.utils.singleton import AlreadyRunningError, WORKER_SINGLETON, "
                    "singleton, worker_control_lock_path\n"
                    "try:\n"
                    "    with singleton(WORKER_SINGLETON, pid_path=worker_control_lock_path(primary=True)):\n"
                    "        raise SystemExit(1)\n"
                    "except AlreadyRunningError:\n"
                    "    raise SystemExit(0)\n"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def check_running() -> None:
        assert flock_is_held(WORKER_SINGLETON, pid_path=db_lock)
        result = second_worker_exit()
        assert result.returncode == 0, result.stderr

    with (
        patch("teatree.loops.worker.LoopWorker") as worker_cls,
        patch("teatree.core.management.commands.worker.signal.signal"),
    ):
        worker_cls.return_value.run.side_effect = check_running
        call_command("worker")
    assert not flock_is_held(WORKER_SINGLETON, pid_path=db_lock)
    assert second_worker_exit().returncode == 1


def test_direct_worker_command_refuses_external_database_under_pytest_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "pytest-of-worker" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("T3_CONTROL_DB_DIR", "/var/lib/teatree/control-db")
    with (
        patch.object(singleton_mod, "singleton", return_value=nullcontext()),
        patch.object(worker_command, "current_generation", return_value=None),
        patch("teatree.utils.singleton_refusals.clear_refusals"),
        patch("teatree.loops.worker.LoopWorker"),
        patch.object(worker_command.signal, "signal"),
        pytest.raises(RuntimeError, match="external control database"),
    ):
        call_command("worker")


def test_deployed_worker_holds_a_separate_health_lock_only_while_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir()
    monkeypatch.setenv("T3_WORKER_HEALTH_DIR", str(health_dir))
    health_lock = health_dir / "worker.pid"

    def check_running() -> None:
        assert flock_is_held("worker-health", pid_path=health_lock)

    with (
        patch("teatree.loops.worker.LoopWorker") as worker_cls,
        patch("teatree.core.management.commands.worker.signal.signal"),
    ):
        worker_cls.return_value.run.side_effect = check_running
        call_command("worker")
    assert not flock_is_held("worker-health", pid_path=health_lock)


def test_second_instance_refuses_with_nonzero_exit() -> None:
    def _raise(_name: str) -> None:
        name = "worker"
        raise AlreadyRunningError(name, 4321, Path(name).with_suffix(".pid"))

    with (
        patch("teatree.utils.singleton.singleton", side_effect=_raise),
        pytest.raises(SystemExit) as exc,
    ):
        call_command("worker")
    assert exc.value.code == 1


def test_a_successful_acquire_forgets_the_standing_streak() -> None:
    record_refusal(WORKER_SINGLETON, fingerprint="foreign")
    with (
        patch("teatree.loops.worker.LoopWorker"),
        patch("teatree.core.management.commands.worker.signal.signal"),
    ):
        call_command("worker")
    assert read_streak(WORKER_SINGLETON) is None


def test_a_refusal_names_where_the_holder_is(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            "teatree.utils.singleton.current_context",
            return_value=ExecutionContext(pid_namespace="pid:[2]", hostname="this-box", role="worker"),
        ),
        patch("teatree.utils.singleton.singleton", side_effect=_refuse_from_outside),
        pytest.raises(SystemExit),
    ):
        call_command("worker")
    assert "NOT resolvable here" in capsys.readouterr().err


def test_the_first_refusals_stay_an_ordinary_nonzero_exit() -> None:
    for _ in range(ESCALATION_THRESHOLD - 1):
        with (
            patch("teatree.utils.singleton.singleton", side_effect=_refuse_from_outside),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("worker")
        assert exc.value.code == 1


def test_repeated_identical_refusals_escalate(capsys: pytest.CaptureFixture[str]) -> None:
    codes: list[int | str | None] = []
    for _ in range(ESCALATION_THRESHOLD):
        with (
            patch("teatree.utils.singleton.singleton", side_effect=_refuse_from_outside),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("worker")
        codes.append(exc.value.code)
    assert codes[-1] == STARVED_EXIT
    escalation = capsys.readouterr().err
    assert f"{ESCALATION_THRESHOLD} times running" in escalation
    assert "t3 doctor check" in escalation


def test_a_changed_reason_restarts_the_count() -> None:
    def _refuse_from_a_sibling(_name: str) -> None:
        sibling = ExecutionContext(pid_namespace="pid:[3]", hostname="box", role="admin")
        raise AlreadyRunningError(WORKER_SINGLETON, 99, Path("worker.pid"), holder_context=sibling)

    for _ in range(ESCALATION_THRESHOLD - 1):
        with (
            patch("teatree.utils.singleton.singleton", side_effect=_refuse_from_outside),
            pytest.raises(SystemExit),
        ):
            call_command("worker")
    with (
        patch("teatree.utils.singleton.singleton", side_effect=_refuse_from_a_sibling),
        pytest.raises(SystemExit) as exc,
    ):
        call_command("worker")
    assert exc.value.code == 1
    streak = read_streak(WORKER_SINGLETON)
    assert streak is not None
    assert streak.count == 1


class TestTheWorkerActivatesItsGeneration(TestCase):
    _SHA = "8" * 40

    def _boot(self, generation: str) -> None:
        with (
            patch.dict("os.environ", {"TEATREE_GENERATION": generation}),
            patch("teatree.loops.worker.LoopWorker"),
            patch("teatree.core.management.commands.worker.signal.signal"),
        ):
            call_command("worker")

    def test_an_image_worker_activates_its_generation(self) -> None:
        self._boot(self._SHA)

        assert WorkerGeneration.objects.state_of(self._SHA) == WorkerGeneration.State.ACTIVE

    def test_a_legacy_worker_registers_nothing(self) -> None:
        self._boot("")

        assert not WorkerGeneration.objects.exists()

    def test_a_refused_worker_activates_nothing(self) -> None:
        with (
            patch.dict("os.environ", {"TEATREE_GENERATION": self._SHA}),
            patch("teatree.utils.singleton.singleton", side_effect=_refuse_from_outside),
            pytest.raises(SystemExit),
        ):
            call_command("worker")

        assert not WorkerGeneration.objects.exists()
