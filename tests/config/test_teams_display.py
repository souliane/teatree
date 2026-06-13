"""``[teams] display`` resolution — the pane-display mode setting (WI-5, #1838).

The presentation-layer toggle for Track-B maker panes: ``[teams] display =
"auto" | "tmux" | "none"`` (default ``"none"``, ships dark). It mirrors the
``teams_enabled`` setting family exactly — the global value reads from the
``[teams]`` table, per-overlay overridable via ``[overlays.<name>].teams_display``,
and ``T3_TEAMS_DISPLAY`` env wins. A typo raises rather than silently selecting a
less-conservative mode.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched.
"""

from pathlib import Path

import pytest

from teatree.config import load_config
from teatree.config.enums import TeamsDisplay
from teatree.config.resolution import get_effective_settings

from ._shared import _write_toml


class TestTeamsDisplayDefaultsOff:
    def test_display_defaults_to_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        assert load_config(cfg).user.teams_display is TeamsDisplay.NONE

    def test_absent_teams_table_resolves_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\nmode = "auto"\n', encoding="utf-8")
        assert load_config(cfg).user.teams_display is TeamsDisplay.NONE


class TestTeamsDisplayGlobalRead:
    def test_global_table_read(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teams]\ndisplay = "tmux"\n', encoding="utf-8")
        assert load_config(cfg).user.teams_display is TeamsDisplay.TMUX

    def test_auto_value(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teams]\ndisplay = "auto"\n', encoding="utf-8")
        assert load_config(cfg).user.teams_display is TeamsDisplay.AUTO

    def test_invalid_value_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teams]\ndisplay = "nope"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid teams display"):
            load_config(cfg)


class TestTeamsDisplayOverrideChain:
    """Per-overlay + env tiers resolve like the rest of the ``teams_*`` family."""

    def test_overlay_override_wins_over_global(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_TEAMS_DISPLAY", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        _write_toml(
            config_file,
            """
[teams]
display = "none"

[overlays.my-overlay]
class = "x.y:Z"
teams_display = "tmux"
""",
        )
        assert get_effective_settings().teams_display is TeamsDisplay.TMUX

    def test_env_var_beats_overlay_override(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_TEAMS_DISPLAY", "none")
        monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        _write_toml(
            config_file,
            """
[teams]
display = "tmux"

[overlays.my-overlay]
class = "x.y:Z"
teams_display = "auto"
""",
        )
        assert get_effective_settings().teams_display is TeamsDisplay.NONE

    def test_env_invalid_falls_safe_to_none(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_TEAMS_DISPLAY", "garbage")
        config_file.write_text('[teams]\ndisplay = "tmux"\n', encoding="utf-8")
        # A mistyped env var must not crash the resolver or escalate the mode:
        # it fails safe to the conservative NONE (display is presentation-only).
        assert get_effective_settings().teams_display is TeamsDisplay.NONE
