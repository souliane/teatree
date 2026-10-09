# test-path: cross-cutting
import re
from pathlib import Path

import pytest

from scripts.hooks import generate_defaults_toml as gen
from teatree.config.cold_defaults import DEFAULTS_TOML


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    committed = DEFAULTS_TOML.read_text(encoding="utf-8")
    drifted = re.sub(r"^artifact_idle_days = 2\.0", "artifact_idle_days = 99.0", committed, count=1, flags=re.MULTILINE)
    assert drifted != committed
    target = tmp_path / "defaults.toml"
    target.write_text(drifted, encoding="utf-8")
    monkeypatch.setattr(gen, "DEFAULTS_TOML", target)
    return target


def test_the_committed_file_is_already_what_the_declarations_render() -> None:
    assert gen.main([]) == 0


def test_drift_is_reported_and_left_in_place(planted: Path) -> None:
    before = planted.read_text(encoding="utf-8")

    assert gen.main([]) == 1
    assert planted.read_text(encoding="utf-8") == before


def test_write_puts_the_declared_value_back(planted: Path) -> None:
    assert gen.main(["--write"]) == 0
    assert planted.read_text(encoding="utf-8") == DEFAULTS_TOML.read_text(encoding="utf-8")
