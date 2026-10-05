"""Spawn path modules required by the model dispatch test suite."""

# test-path: cross-cutting
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "teatree"

# The spawn-path modules that must route through resolve_spawn_model only.
_SPAWN_PATH_MODULES = (
    _SRC_ROOT / "agents" / "runner.py",
    _SRC_ROOT / "core" / "management" / "commands" / "loop_dispatch.py",
)


class TestSpawnModelChokepoint:
    def test_spawn_path_modules_exist(self) -> None:
        for module in _SPAWN_PATH_MODULES:
            assert module.is_file(), module
