"""Tests for the on-behalf posting pre-gate policy.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched to them.
No mocks — `load_config` / `get_effective_settings` exercised end-to-end.
"""

from pathlib import Path

import pytest

from teatree.on_behalf_gate import ask_before_post_on_behalf_enabled


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def test_enabled_by_default_when_no_config(config_file: Path) -> None:
    assert ask_before_post_on_behalf_enabled() is True


def test_enabled_by_default_when_section_present_but_unset(config_file: Path) -> None:
    config_file.write_text("[teatree]\n", encoding="utf-8")
    assert ask_before_post_on_behalf_enabled() is True


def test_explicit_false_disables_the_gate(config_file: Path) -> None:
    config_file.write_text("[teatree]\nask_before_post_on_behalf = false\n", encoding="utf-8")
    assert ask_before_post_on_behalf_enabled() is False


def test_explicit_true_keeps_the_gate(config_file: Path) -> None:
    config_file.write_text("[teatree]\nask_before_post_on_behalf = true\n", encoding="utf-8")
    assert ask_before_post_on_behalf_enabled() is True


def test_per_overlay_override_wins_over_global(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A trusted overlay can opt out of the gate without flipping the global."""
    config_file.write_text(
        "[teatree]\n"
        "ask_before_post_on_behalf = true\n"
        "[overlays.trusted]\n"
        'overlay_class = "x.Y"\n'
        "ask_before_post_on_behalf = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
    assert ask_before_post_on_behalf_enabled() is False
