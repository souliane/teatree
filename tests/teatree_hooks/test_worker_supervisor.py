"""hooks.scripts.worker_supervisor — SessionStart worker resurrection (#1796).

The decision logic is tested with injected collaborators (no real flock or
subprocess): spawn only when the flock is free, and fail-open to a no-op on any
error. ``main`` never raises into the SessionStart hook.
"""

# test-path: cross-cutting — the subject ``worker_supervisor`` lives in
# ``hooks/scripts/``, not under ``src/teatree/<pkg>``, so it mirrors no single package.

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.scripts import worker_supervisor as supervisor
from teatree.utils import singleton


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_spawns_when_the_flock_is_free() -> None:
    spawn = _Spy()
    assert supervisor.resurrect_worker(flock_free=lambda: True, spawn=spawn) == "spawned"
    assert spawn.calls == 1


def test_no_spawn_when_the_flock_is_held() -> None:
    spawn = _Spy()
    assert supervisor.resurrect_worker(flock_free=lambda: False, spawn=spawn) == "already-running"
    assert spawn.calls == 0


def test_remote_target_file_skips_probe_and_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    (tmp_path / "factory-target").write_text("factory.example\n")
    spawn = _Spy()
    probe = _Spy()
    assert supervisor.resurrect_worker(flock_free=probe, spawn=spawn) == "remote-factory"
    assert probe.calls == spawn.calls == 0


def test_remote_environment_skips_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_FACTORY_TARGET", "factory.example")
    spawn = _Spy()
    assert supervisor.resurrect_worker(flock_free=lambda: True, spawn=spawn) == "remote-factory"
    assert spawn.calls == 0


def test_local_environment_overrides_remote_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    (tmp_path / "factory-target").write_text("factory.example\n")
    monkeypatch.setenv("T3_FACTORY_TARGET", "local")
    spawn = _Spy()
    assert supervisor.resurrect_worker(flock_free=lambda: True, spawn=spawn) == "spawned"
    assert spawn.calls == 1


def test_script_import_works_without_pytest_pythonpath(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["T3_FACTORY_TARGET"] = "remote.example"
    env["PATH"] = str(tmp_path)
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "hooks/scripts/worker_supervisor.py")],
        cwd=tmp_path,
        env=env,
        input="{}",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_fails_open_when_spawn_raises() -> None:
    def boom() -> None:
        msg = "no t3 on PATH"
        raise OSError(msg)

    assert supervisor.resurrect_worker(flock_free=lambda: True, spawn=boom) == "error"


def test_fails_open_when_the_flock_probe_raises() -> None:
    def boom() -> bool:
        msg = "probe failed"
        raise RuntimeError(msg)

    spawn = _Spy()
    assert supervisor.resurrect_worker(flock_free=boom, spawn=spawn) == "error"
    assert spawn.calls == 0


def test_an_unreadable_flock_reads_as_held_so_no_duplicate_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_name: str) -> bool:
        msg = "lock dir unreadable"
        raise OSError(msg)

    monkeypatch.setattr(singleton, "flock_is_held", boom)
    assert supervisor._flock_is_free() is False


def test_database_lock_blocks_resurrection_under_another_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "control" / "db.sqlite3"
    home = tmp_path / "other-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("T3_CONTROL_DB_DIR", str(database.parent))
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "config.sqlite3"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(singleton, "DATA_DIR", tmp_path / "host-lock")
    db_lock = database.with_name(f"{database.name}.worker.pid")
    with singleton.singleton(singleton.WORKER_SINGLETON, pid_path=db_lock):
        assert supervisor._flock_is_free() is False


def test_resurrection_from_worktree_probes_spawned_workers_primary_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "other-home"
    home.mkdir()
    control = tmp_path / "control"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("T3_CONTROL_DB_DIR", str(control))
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(singleton, "DATA_DIR", tmp_path / "host-lock")
    primary_lock = control / "db.sqlite3.worker.pid"
    with singleton.singleton(singleton.WORKER_SINGLETON, pid_path=primary_lock):
        assert supervisor._flock_is_free() is False


def test_main_drains_stdin_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = io.StringIO('{"session_id": "s1"}')
    monkeypatch.setattr(sys, "argv", ["worker_supervisor.py", "--event", "SessionStart"])
    monkeypatch.setattr(sys, "stdin", stdin)
    with patch.object(supervisor, "resurrect_worker", return_value="error") as resurrect:
        assert supervisor.main() == 0
    resurrect.assert_called_once_with()
    assert stdin.read() == ""
