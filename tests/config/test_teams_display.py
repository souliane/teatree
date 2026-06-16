# test-path: cross-cutting
"""``teams_display`` resolution — the pane-display mode setting (WI-5, #1838).

The presentation-layer toggle for Track-B maker panes: ``auto`` / ``tmux`` /
``none`` (default ``none``, ships dark). It is DB-home under the #1775 partition
— the global value resolves from a ``ConfigSetting`` row, per-overlay overridable
via an overlay-scoped row, and ``T3_TEAMS_DISPLAY`` env wins. A typo in a stored
row raises on read rather than silently selecting a less-conservative mode; a
mistyped env value fails safe to ``none``.

Integration-first per the Test-Writing Doctrine: real TOML under ``tmp_path``,
DB-home overrides via the real ``ConfigSetting`` store.
"""

from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import load_config
from teatree.config.enums import TeamsDisplay
from teatree.config.resolution import get_effective_settings
from teatree.core.models import ConfigSetting

from ._shared import _write_toml


class TestTeamsDisplayDefaultsOff:
    def test_display_defaults_to_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        assert load_config(cfg).user.teams_display is TeamsDisplay.NONE


class TestTeamsDisplayDbResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_TEAMS_DISPLAY", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(self.config_path, '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\n')
        self.monkeypatch = monkeypatch

    def test_global_db_row_tmux(self) -> None:
        ConfigSetting.objects.set_value("teams_display", "tmux")
        assert get_effective_settings().teams_display is TeamsDisplay.TMUX

    def test_global_db_row_auto(self) -> None:
        ConfigSetting.objects.set_value("teams_display", "auto")
        assert get_effective_settings().teams_display is TeamsDisplay.AUTO

    def test_corrupt_db_value_raises_loud_on_read(self) -> None:
        ConfigSetting.objects.set_value("teams_display", "nope")
        with pytest.raises(ValueError, match="teams_display"):
            get_effective_settings()

    def test_overlay_scoped_row_wins_over_global(self) -> None:
        ConfigSetting.objects.set_value("teams_display", "none")
        ConfigSetting.objects.set_value("teams_display", "tmux", scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().teams_display is TeamsDisplay.TMUX

    def test_env_var_beats_overlay_db_row(self) -> None:
        ConfigSetting.objects.set_value("teams_display", "tmux")
        ConfigSetting.objects.set_value("teams_display", "auto", scope="my-overlay")
        self.monkeypatch.setenv("T3_TEAMS_DISPLAY", "none")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().teams_display is TeamsDisplay.NONE

    def test_env_invalid_falls_safe_to_none(self) -> None:
        ConfigSetting.objects.set_value("teams_display", "tmux")
        self.monkeypatch.setenv("T3_TEAMS_DISPLAY", "garbage")
        # A mistyped env var must not crash the resolver or escalate the mode:
        # it fails safe to the conservative NONE (display is presentation-only).
        assert get_effective_settings().teams_display is TeamsDisplay.NONE
