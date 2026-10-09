"""The shared hook-state resolver.

Pins finding 9 (one resolver for all hook state, no scatter across
``~/.teatree`` / ``~/.cache`` / the data dir).
"""

from pathlib import Path

import pytest

from teatree.hooks import _hook_state
from teatree.paths import DATA_DIR


class TestHookStateRoot:
    def test_t3_data_dir_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "state"))
        assert _hook_state.hook_state_root() == tmp_path / "state"

    def test_falls_back_to_canonical_data_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_DATA_DIR", raising=False)
        assert _hook_state.hook_state_root() == DATA_DIR
