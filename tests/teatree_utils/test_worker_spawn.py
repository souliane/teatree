# test-path: cross-cutting
"""The ONE detached ``t3 worker`` spawner (#1796 / PR-28).

Shared by the SessionStart supervisor and ``t3 worker ensure`` — the fixed argv,
detached session leader, and the absent-``t3`` no-op are pinned so the two callers
can never diverge. The shell-out routes through ``teatree.utils.run.spawn_session_leader``.
"""

import pytest

from teatree.utils import worker_spawn


def test_returns_false_when_t3_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_spawn.shutil, "which", lambda _name: None)
    calls: list[object] = []
    monkeypatch.setattr(worker_spawn, "spawn_session_leader", lambda *a, **k: calls.append((a, k)))
    assert worker_spawn.spawn_detached_worker() is False
    assert calls == []  # never spawns when there is no `t3`


def test_spawns_a_detached_worker_with_fixed_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_spawn.shutil, "which", lambda _name: "/usr/local/bin/t3")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_spawn(cmd: list[str], **kwargs: object) -> None:
        calls.append((cmd, kwargs))

    monkeypatch.setattr(worker_spawn, "spawn_session_leader", _fake_spawn)
    assert worker_spawn.spawn_detached_worker() is True
    ((cmd, kwargs),) = calls
    assert cmd == ["/usr/local/bin/t3", "worker"]
    assert kwargs["stdout"] is worker_spawn.DEVNULL  # detached streams
