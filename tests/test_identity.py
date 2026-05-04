"""Tests for the user-on-behalf signature policy.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched to them.
No mocks — `load_config` is exercised end-to-end.
"""

from pathlib import Path

import pytest

from teatree.identity import agent_signature_enabled, agent_signature_suffix


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def test_signature_disabled_by_default(config_file: Path) -> None:
    config_file.write_text("[teatree]\n", encoding="utf-8")
    assert agent_signature_enabled() is False
    assert agent_signature_suffix("\n— Sent using Claude") == ""


def test_signature_disabled_when_no_config(config_file: Path) -> None:
    assert agent_signature_enabled() is False
    assert agent_signature_suffix("\nCo-Authored-By: agent <a@b>") == ""


def test_signature_enabled_passes_suffix_through(config_file: Path) -> None:
    config_file.write_text("[teatree]\nagent_signature = true\n", encoding="utf-8")
    assert agent_signature_enabled() is True
    assert agent_signature_suffix("\n— from the assistant") == "\n— from the assistant"
