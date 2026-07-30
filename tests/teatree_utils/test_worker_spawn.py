# test-path: cross-cutting
"""The ONE detached ``t3 worker`` spawner (#1796 / PR-28).

Shared by the SessionStart supervisor and ``t3 worker ensure`` — the fixed argv,
detached session leader, and the absent-``t3`` no-op are pinned so the two callers
can never diverge. The shell-out routes through ``teatree.utils.run.spawn_session_leader``.
The child's stderr lands in a per-spawn truncated log, so a worker that dies during
startup leaves something to read instead of vanishing into ``DEVNULL``.
"""

from pathlib import Path
from typing import IO

import pytest

from teatree.utils import worker_spawn
from teatree.utils.worker_spawn import read_spawn_log_tail


def test_returns_false_when_t3_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_spawn.shutil, "which", lambda _name: None)
    calls: list[object] = []
    monkeypatch.setattr(worker_spawn, "spawn_session_leader", lambda *a, **k: calls.append((a, k)))
    assert worker_spawn.spawn_detached_worker() is False
    assert calls == []  # never spawns when there is no `t3`


def test_spawns_a_detached_worker_with_fixed_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker_spawn, "SPAWN_LOG_PATH", tmp_path / "worker-spawn.log")
    monkeypatch.setattr(worker_spawn.shutil, "which", lambda _name: "/usr/local/bin/t3")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_spawn(cmd: list[str], **kwargs: object) -> None:
        calls.append((cmd, kwargs))

    monkeypatch.setattr(worker_spawn, "spawn_session_leader", _fake_spawn)
    assert worker_spawn.spawn_detached_worker() is True
    ((cmd, kwargs),) = calls
    assert cmd == ["/usr/local/bin/t3", "worker"]
    assert kwargs["stdout"] is worker_spawn.DEVNULL  # detached streams


def test_the_child_stderr_is_captured_not_discarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = tmp_path / "worker-spawn.log"
    log.write_text("output from the worker before this one\n", encoding="utf-8")
    monkeypatch.setattr(worker_spawn, "SPAWN_LOG_PATH", log)
    monkeypatch.setattr(worker_spawn.shutil, "which", lambda _name: "/usr/local/bin/t3")

    def _fake_spawn(_cmd: list[str], *, stderr: IO[str], **_kwargs: object) -> None:
        stderr.write("ModuleNotFoundError: No module named 'teatree'\n")

    monkeypatch.setattr(worker_spawn, "spawn_session_leader", _fake_spawn)
    assert worker_spawn.spawn_detached_worker() is True

    # A startup crash is readable instead of vanishing into DEVNULL ...
    assert read_spawn_log_tail() == "ModuleNotFoundError: No module named 'teatree'"
    # ... and the log holds THIS spawn only, so it cannot grow across restarts.
    assert "before this one" not in log.read_text(encoding="utf-8")


def test_the_tail_is_empty_when_no_spawn_log_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker_spawn, "SPAWN_LOG_PATH", tmp_path / "absent.log")
    assert read_spawn_log_tail() == ""


def test_the_tail_keeps_only_the_last_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = tmp_path / "worker-spawn.log"
    log.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
    monkeypatch.setattr(worker_spawn, "SPAWN_LOG_PATH", log)
    assert read_spawn_log_tail(lines=3) == "line 47\nline 48\nline 49"
