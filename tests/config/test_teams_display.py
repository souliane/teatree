# test-path: cross-cutting
"""``teams_display`` resolution — the pane-display mode setting (WI-5, #1838).

The presentation-layer toggle for Track-B maker panes: ``auto`` / ``tmux`` /
``none`` (default ``none``, ships dark). It is DB-home — the global value resolves
from a ``ConfigSetting`` row, per-overlay overridable via an overlay-scoped row, and
``T3_TEAMS_DISPLAY`` env wins. A typo in a stored row raises on read rather than
silently selecting a less-conservative mode; a mistyped env value fails safe to
``none``.

Integration-first per the Test-Writing Doctrine: DB-home overrides via the real
``ConfigSetting`` store.
"""

import pytest
from django.test import TestCase

from teatree.config import load_config
from teatree.config.enums import TeamsDisplay
from teatree.config.resolution import get_effective_settings
from teatree.core.models import ConfigSetting


class TestTeamsDisplayDefaultsOff:
    def test_display_defaults_to_none(self) -> None:
        assert load_config().user.teams_display is TeamsDisplay.NONE


class TestTeamsDisplayDbResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_TEAMS_DISPLAY", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
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
