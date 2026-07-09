"""Config knobs for the idle-stack reaper + acquisition queue (#2190)."""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, discover_overlays, get_effective_settings
from teatree.core.models import ConfigSetting
from tests.config._shared import _seed_config_db


class TestDefaults(TestCase):
    """The five knobs ship with the spec's safe defaults."""

    def test_reaper_enabled_by_default(self) -> None:
        assert UserSettings().idle_stack_reaper_disabled is False

    def test_queue_enabled_by_default(self) -> None:
        assert UserSettings().local_stack_queue_disabled is False

    def test_idle_minutes_default(self) -> None:
        assert UserSettings().idle_stack_idle_minutes == 30

    def test_reaper_cadence_default(self) -> None:
        assert UserSettings().idle_stack_reaper_cadence_minutes == 5

    def test_queue_max_attempts_default(self) -> None:
        assert UserSettings().local_stack_queue_max_attempts == 13


class TestParsing(TestCase):
    """The DB-home knobs resolve from the ``ConfigSetting`` store (#1775)."""

    def test_parses_all_knobs(self) -> None:
        ConfigSetting.objects.set_value("idle_stack_reaper_disabled", value=True)
        ConfigSetting.objects.set_value("idle_stack_idle_minutes", 45)
        ConfigSetting.objects.set_value("idle_stack_reaper_cadence_minutes", 10)
        ConfigSetting.objects.set_value("local_stack_queue_disabled", value=True)
        ConfigSetting.objects.set_value("local_stack_queue_max_attempts", 8)
        settings = get_effective_settings()
        assert settings.idle_stack_reaper_disabled is True
        assert settings.idle_stack_idle_minutes == 45
        assert settings.idle_stack_reaper_cadence_minutes == 10
        assert settings.local_stack_queue_disabled is True
        assert settings.local_stack_queue_max_attempts == 8

    def test_missing_knobs_use_defaults(self) -> None:
        # No DB rows -> the dataclass defaults (no TOML tier for a DB-home key).
        settings = get_effective_settings()
        assert settings.idle_stack_reaper_disabled is False
        assert settings.idle_stack_idle_minutes == 30


class TestOverlayOverridable(TestCase):
    """All five knobs are per-overlay overridable (mirrors resource_pressure)."""

    def test_all_knobs_overridable(self) -> None:
        for key in (
            "idle_stack_reaper_disabled",
            "idle_stack_idle_minutes",
            "idle_stack_reaper_cadence_minutes",
            "local_stack_queue_disabled",
            "local_stack_queue_max_attempts",
        ):
            assert key in OVERLAY_OVERRIDABLE_SETTINGS


def test_overlay_registry_entry_carries_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A per-overlay override in the ``overlays`` registry entry is parsed by
    # discovery into the entry overrides (the effective-settings layer applies it).
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    db = tmp_path / "config.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    _seed_config_db(db, overlays={"heavy": {"class": "x.y:Z", "idle_stack_idle_minutes": 60}})

    entries = {e.name: e for e in discover_overlays()}
    assert entries["heavy"].overrides["idle_stack_idle_minutes"] == 60
