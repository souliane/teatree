"""manage.py worker — the singleton loop-timer worker command (#1796).

The command wires the flock singleton + SIGTERM/SIGINT handlers around the
:class:`teatree.loops.worker.LoopWorker`; a second invocation while one holds the
flock refuses with a non-zero exit. Collaborators are stubbed so the test never
spawns a real, forever-blocking worker or mutates the process's signal handlers.

Losing the race is also RECORDED (#3976): the supervisor restarts the refused worker
forever, so the count of consecutive identical refusals is the only thing that can tell
a deploy hand-over apart from a race this deployment can never win.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.management.commands.worker import STARVED_EXIT
from teatree.utils.singleton import WORKER_SINGLETON, AlreadyRunningError, ExecutionContext
from teatree.utils.singleton_refusals import ESCALATION_THRESHOLD, read_streak, record_refusal

_OUTSIDE_HOLDER = ExecutionContext(pid_namespace="pid:[1]", hostname="box", role="")


@pytest.fixture(autouse=True)
def _singleton_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the lock file AND the refusal ledger at a per-test dir.

    The tests that let the command really acquire the singleton would otherwise all
    race on one shared lock file, so two xdist workers refuse each other.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("teatree.utils.singleton.DATA_DIR", data)
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
