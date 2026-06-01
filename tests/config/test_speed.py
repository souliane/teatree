"""The ``speed`` parallel-work throughput dial.

A single ordered dial — ``slow`` < ``medium`` < ``full`` < ``boost`` (default
``medium``) — governing how many threads of work the orchestrator drives at
once. Orthogonal to ``mode``/``autonomy`` (those gate *whether* a publish
proceeds; this governs *how many* threads run) and never relaxes a safety
gate. Resolved through the generic ``get_effective_settings`` layer: env
(``T3_SPEED``) > per-overlay ``[overlays.<name>]`` > global ``[teatree]`` >
the ``UserSettings`` default.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched.
"""

from pathlib import Path

import pytest

from teatree.config import Speed, get_effective_settings, load_config

from ._shared import _write_toml


class TestSpeedParse:
    def test_parse_slow(self) -> None:
        assert Speed.parse("slow") is Speed.SLOW

    def test_parse_medium(self) -> None:
        assert Speed.parse("medium") is Speed.MEDIUM

    def test_parse_full(self) -> None:
        assert Speed.parse("full") is Speed.FULL

    def test_parse_boost(self) -> None:
        assert Speed.parse("boost") is Speed.BOOST

    def test_parse_is_case_insensitive_and_strips(self) -> None:
        assert Speed.parse("  FULL ") is Speed.FULL

    def test_alias_low_maps_to_slow(self) -> None:
        assert Speed.parse("low") is Speed.SLOW

    def test_alias_normal_maps_to_medium(self) -> None:
        assert Speed.parse("normal") is Speed.MEDIUM

    def test_alias_high_maps_to_full(self) -> None:
        assert Speed.parse("high") is Speed.FULL

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid speed"):
            Speed.parse("ludicrous")

    def test_invalid_message_lists_values_and_aliases(self) -> None:
        with pytest.raises(ValueError, match="aliases: high, low, normal"):
            Speed.parse("ludicrous")

    def test_tier_ordering_slow_medium_full_boost(self) -> None:
        """Documented dial ordering: slow < medium < full < boost (default medium)."""
        assert list(Speed) == [Speed.SLOW, Speed.MEDIUM, Speed.FULL, Speed.BOOST]


class TestSpeedDefault:
    def test_defaults_to_medium(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.speed is Speed.MEDIUM

    def test_missing_file_defaults_to_medium(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "nonexistent.toml").user.speed is Speed.MEDIUM


class TestSpeedGlobalResolution:
    def test_global_speed_full(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nspeed = "full"\n')
        assert load_config(config_path).user.speed is Speed.FULL

    def test_global_alias_high_is_full(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nspeed = "high"\n')
        assert load_config(config_path).user.speed is Speed.FULL

    def test_global_typo_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nspeed = "ludicrous"\n')
        with pytest.raises(ValueError, match="Invalid speed"):
            load_config(config_path)


class TestSpeedEffectiveResolution:
    def test_per_overlay_override_wins_over_global(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_SPEED", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        _write_toml(
            config_file,
            '[teatree]\nspeed = "slow"\n[overlays.fast]\nspeed = "boost"\n',
        )
        assert get_effective_settings().speed is Speed.BOOST

    def test_env_wins_over_per_overlay_and_global(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        monkeypatch.setenv("T3_SPEED", "slow")
        _write_toml(
            config_file,
            '[teatree]\nspeed = "full"\n[overlays.fast]\nspeed = "boost"\n',
        )
        assert get_effective_settings().speed is Speed.SLOW

    def test_env_alias_resolves(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_SPEED", "high")
        _write_toml(config_file, "[teatree]\n")
        assert get_effective_settings().speed is Speed.FULL

    def test_one_overlay_speed_does_not_leak_to_another(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_SPEED", raising=False)
        _write_toml(
            config_file,
            '[teatree]\n[overlays.fast]\nspeed = "boost"\n[overlays.careful]\nspeed = "slow"\n',
        )

        monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        assert get_effective_settings().speed is Speed.SLOW

        monkeypatch.setenv("T3_OVERLAY_NAME", "fast")
        assert get_effective_settings().speed is Speed.BOOST
