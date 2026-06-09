"""Config knobs for the idle-stack reaper + acquisition queue (#2190)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, discover_overlays, load_config


def _write(body: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix="idle_cfg_")) / ".teatree.toml"
    path.write_text(body, encoding="utf-8")
    return path


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
    """``load_config`` parses the knobs from ``[teatree]``."""

    def test_parses_all_knobs(self) -> None:
        path = _write(
            "[teatree]\n"
            "idle_stack_reaper_disabled = true\n"
            "idle_stack_idle_minutes = 45\n"
            "idle_stack_reaper_cadence_minutes = 10\n"
            "local_stack_queue_disabled = true\n"
            "local_stack_queue_max_attempts = 8\n",
        )
        settings = load_config(path).user
        assert settings.idle_stack_reaper_disabled is True
        assert settings.idle_stack_idle_minutes == 45
        assert settings.idle_stack_reaper_cadence_minutes == 10
        assert settings.local_stack_queue_disabled is True
        assert settings.local_stack_queue_max_attempts == 8

    def test_missing_knobs_use_defaults(self) -> None:
        path = _write("[teatree]\nloop_cadence_seconds = 60\n")
        settings = load_config(path).user
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

    def test_per_overlay_override_wins(self) -> None:
        path = _write(
            "[teatree]\n"
            "idle_stack_idle_minutes = 30\n\n"
            "[overlays.heavy]\n"
            'class = "x.y:Z"\n'
            "idle_stack_idle_minutes = 60\n",
        )
        assert load_config(path).user.idle_stack_idle_minutes == 30
        entries = {e.name: e for e in discover_overlays(config_path=path)}
        # The overlay entry carries the override the effective-settings layer applies.
        assert entries["heavy"].overrides["idle_stack_idle_minutes"] == 60
