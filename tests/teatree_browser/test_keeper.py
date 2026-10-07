import os
import signal
import time
from pathlib import Path

import pytest

from teatree.browser import keeper as keeper_module
from teatree.browser.keeper import IDLE_TIMEOUT_S, Keeper
from teatree.browser.state import SessionFiles


@pytest.fixture
def keeper(tmp_path: Path) -> Keeper:
    files = SessionFiles(tmp_path / "session")
    files.root.mkdir()
    files.last_used.touch()
    return Keeper(files)


def test_a_freshly_used_session_keeps_running(keeper: Keeper) -> None:
    assert keeper._should_stop() is False


def test_the_stop_file_ends_the_keeper(keeper: Keeper) -> None:
    keeper.files.stop.touch()

    assert keeper._should_stop() is True


def test_sigterm_ends_the_keeper(keeper: Keeper) -> None:
    keeper._on_sigterm(signal.SIGTERM, None)

    assert keeper._should_stop() is True


def test_a_session_idle_past_the_timeout_ends_the_keeper(keeper: Keeper) -> None:
    long_ago = time.time() - IDLE_TIMEOUT_S - 1
    os.utime(keeper.files.last_used, (long_ago, long_ago))

    assert keeper._should_stop() is True


def test_a_removed_session_directory_ends_the_keeper(keeper: Keeper) -> None:
    keeper.files.last_used.unlink()

    assert keeper._should_stop() is True


def test_a_second_keeper_for_a_held_session_stands_down(keeper: Keeper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keeper_module, "_LOCK_ATTEMPTS", 2)
    monkeypatch.setattr(keeper_module.signal, "signal", lambda *_args: None)
    launched: list[bool] = []
    monkeypatch.setattr(Keeper, "_launch_and_serve", lambda _self: launched.append(True) or 0)

    with keeper_module.singleton(keeper_module.KEEPER_LOCK, pid_path=keeper.files.pid):
        assert Keeper(keeper.files).run() == 0

    assert launched == []
    assert Keeper(keeper.files).run() == 0
    assert launched == [True]
