"""hooks.scripts.loop_runner_supervisor — SessionStart daemon resurrection (#2876 decision 2b).

The decision logic is tested with injected collaborators (no real Django, flock, or
subprocess): spawn only when enabled AND the flock is free, and fail-open to a no-op
on any error. ``main`` never raises into the SessionStart hook.
"""

import sys
from pathlib import Path
from unittest.mock import patch

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import loop_runner_supervisor as supervisor  # noqa: E402


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_spawns_when_enabled_and_flock_free() -> None:
    spawn = _Spy()
    action = supervisor.resurrect_loop_runner(enabled=lambda: True, flock_free=lambda: True, spawn=spawn)
    assert action == "spawned"
    assert spawn.calls == 1


def test_no_spawn_when_disabled() -> None:
    spawn = _Spy()
    action = supervisor.resurrect_loop_runner(enabled=lambda: False, flock_free=lambda: True, spawn=spawn)
    assert action == "disabled"
    assert spawn.calls == 0


def test_no_spawn_when_flock_held() -> None:
    spawn = _Spy()
    action = supervisor.resurrect_loop_runner(enabled=lambda: True, flock_free=lambda: False, spawn=spawn)
    assert action == "already-running"
    assert spawn.calls == 0


def test_fails_open_when_spawn_raises() -> None:
    def boom() -> None:
        msg = "no t3 on PATH"
        raise OSError(msg)

    action = supervisor.resurrect_loop_runner(enabled=lambda: True, flock_free=lambda: True, spawn=boom)
    assert action == "error"


def test_main_drains_stdin_and_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["loop_runner_supervisor.py", "--event", "SessionStart"])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"session_id": "s1"}'))
    with patch.object(supervisor, "resurrect_loop_runner", return_value="disabled") as resurrect:
        assert supervisor.main() == 0
    resurrect.assert_called_once_with()
