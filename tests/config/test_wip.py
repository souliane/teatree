"""The ``wip`` bounded-WIP throughput dial.

A single ordered dial — ``slow`` < ``medium`` < ``full`` < ``boost`` (default
``medium``) — governing how much new work a loop tick admits at once.
Orthogonal to ``mode``/``autonomy`` (those gate *whether* a publish proceeds;
this governs *how many* threads run) and never relaxes a safety gate. Resolved
through the generic ``get_effective_settings`` layer: env (``T3_WIP``) >
per-overlay ``[overlays.<name>]`` > global ``[teatree]`` > the ``UserSettings``
default.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched.
"""

from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import Wip, get_effective_settings, load_config
from teatree.core.models import ConfigSetting

from ._shared import _write_toml


class TestWipParse:
    def test_parse_slow(self) -> None:
        assert Wip.parse("slow") is Wip.SLOW

    def test_parse_medium(self) -> None:
        assert Wip.parse("medium") is Wip.MEDIUM

    def test_parse_full(self) -> None:
        assert Wip.parse("full") is Wip.FULL

    def test_parse_boost(self) -> None:
        assert Wip.parse("boost") is Wip.BOOST

    def test_parse_is_case_insensitive_and_strips(self) -> None:
        assert Wip.parse("  FULL ") is Wip.FULL

    def test_alias_low_maps_to_slow(self) -> None:
        assert Wip.parse("low") is Wip.SLOW

    def test_alias_normal_maps_to_medium(self) -> None:
        assert Wip.parse("normal") is Wip.MEDIUM

    def test_alias_high_maps_to_full(self) -> None:
        assert Wip.parse("high") is Wip.FULL

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid wip"):
            Wip.parse("ludicrous")

    def test_invalid_message_lists_values_and_aliases(self) -> None:
        with pytest.raises(ValueError, match="aliases: high, low, normal"):
            Wip.parse("ludicrous")

    def test_tier_ordering_slow_medium_full_boost(self) -> None:
        """Documented dial ordering: slow < medium < full < boost (default medium)."""
        assert list(Wip) == [Wip.SLOW, Wip.MEDIUM, Wip.FULL, Wip.BOOST]


class TestWipDefault:
    def test_defaults_to_medium(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.wip is Wip.MEDIUM

    def test_missing_file_defaults_to_medium(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "nonexistent.toml").user.wip is Wip.MEDIUM


class TestWipDbResolution(TestCase):
    """``wip`` is DB-home (#1775): it resolves from a ``ConfigSetting`` row.

    The DB twin of the old ``[teatree] wip`` / ``[overlays.<name>] wip``.
    """

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_WIP", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(
            self.config_path,
            '[teatree]\n\n[overlays.fast]\nclass = "x:Y"\n\n[overlays.careful]\nclass = "x:Y"\n',
        )
        self.monkeypatch = monkeypatch

    def test_global_db_row_full(self) -> None:
        ConfigSetting.objects.set_value("wip", "full")
        assert get_effective_settings().wip is Wip.FULL

    def test_global_db_alias_high_is_full(self) -> None:
        ConfigSetting.objects.set_value("wip", "high")
        assert get_effective_settings().wip is Wip.FULL

    def test_corrupt_db_value_raises_loud_on_read(self) -> None:
        # An out-of-band corrupt row (the write path validates, so this can only
        # exist via a direct ORM write) raises LOUD on read, never silently.
        ConfigSetting.objects.set_value("wip", "ludicrous")
        with pytest.raises(ValueError, match="wip"):
            get_effective_settings()

    def test_overlay_scoped_db_row_wins_over_global(self) -> None:
        ConfigSetting.objects.set_value("wip", "slow")
        ConfigSetting.objects.set_value("wip", "boost", scope="fast")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        assert get_effective_settings().wip is Wip.BOOST

    def test_env_wins_over_overlay_and_global_db(self) -> None:
        ConfigSetting.objects.set_value("wip", "full")
        ConfigSetting.objects.set_value("wip", "boost", scope="fast")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        self.monkeypatch.setenv("T3_WIP", "slow")
        assert get_effective_settings().wip is Wip.SLOW

    def test_env_alias_resolves(self) -> None:
        self.monkeypatch.setenv("T3_WIP", "high")
        assert get_effective_settings().wip is Wip.FULL

    def test_one_overlay_wip_does_not_leak_to_another(self) -> None:
        ConfigSetting.objects.set_value("wip", "boost", scope="fast")
        ConfigSetting.objects.set_value("wip", "slow", scope="careful")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        assert get_effective_settings().wip is Wip.SLOW
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        assert get_effective_settings().wip is Wip.BOOST
