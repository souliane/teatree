"""Wiring tests for the resource-pressure scanner (#128).

Covers the dispatch routing (``resource.*`` → the right action kind/zone),
the config knobs + their defaults, and the ``build_default_jobs`` global
registration + kill-switch.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal


class DispatchRoutingTests(TestCase):
    """``resource.*`` signals route to the mechanical handler or the statusline."""

    def test_cleanup_needed_routes_to_free_resources_mechanical(self) -> None:
        signal = ScanSignal(kind="resource.cleanup_needed", summary="disk crit", payload={"resource": "disk"})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "mechanical"
        assert actions[0].zone == "free_resources"

    def test_pressure_warn_routes_to_action_needed_statusline_only(self) -> None:
        signal = ScanSignal(kind="resource.pressure_warn", summary="disk warn", payload={"resource": "disk"})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "statusline"
        assert actions[0].zone == "action_needed"

    def test_cleanup_failed_routes_to_action_needed_statusline(self) -> None:
        signal = ScanSignal(kind="resource.cleanup_failed", summary="purge failed", payload={})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "statusline"
        assert actions[0].zone == "action_needed"

    def test_ram_kill_candidate_is_statusline_only_never_agent(self) -> None:
        signal = ScanSignal(kind="resource.ram_kill_candidate", summary="kill candidate", payload={})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "statusline", "a flagged process-kill must never be an autonomous agent action"


class ConfigDefaultsTests(TestCase):
    """The config knobs ship with the spec's safe defaults."""

    def _settings(self) -> object:
        from teatree.config import UserSettings  # noqa: PLC0415

        return UserSettings()

    def test_scanner_enabled_by_default(self) -> None:
        assert self._settings().resource_pressure_disabled is False

    def test_destructive_flags_default_off(self) -> None:
        settings = self._settings()
        assert settings.allow_destructive_disk is False
        assert settings.allow_destructive_ram is False

    def test_threshold_defaults(self) -> None:
        settings = self._settings()
        assert settings.disk_warn_free_gb == pytest.approx(25.0)
        assert settings.disk_crit_free_gb == pytest.approx(10.0)
        assert settings.ram_warn_avail_gb == pytest.approx(3.0)
        assert settings.ram_crit_avail_gb == pytest.approx(1.5)

    def test_cadence_and_rate_limit_defaults(self) -> None:
        settings = self._settings()
        assert settings.resource_pressure_cadence_minutes == 5
        assert settings.resource_pressure_min_free_interval_minutes == 30

    def test_default_cache_allowlist_excludes_prek_and_projects(self) -> None:
        allowlist = self._settings().disk_cache_allowlist
        assert "~/.cache/pre-commit" in allowlist
        assert "~/.cache/codex-runtimes" in allowlist
        assert "~/.cache/prek" not in allowlist, "prek has unknown rebuild semantics — never default-purged"
        assert "~/.claude/projects" not in allowlist, "session memory is never an auto-purge target"

    def test_ram_kill_allowlist_defaults_empty(self) -> None:
        assert self._settings().ram_kill_allowlist == []

    def test_worktree_gc_caps(self) -> None:
        settings = self._settings()
        assert settings.worktree_stale_days == 30
        assert settings.max_worktree_gc_per_tick == 3


class ConfigParsingTests(TestCase):
    """``load_config`` parses the knobs from ``[teatree]`` and applies overrides."""

    def _write_config(self, body: str) -> Path:
        import tempfile  # noqa: PLC0415

        path = Path(tempfile.mkdtemp(prefix="rp_cfg_")) / ".teatree.toml"
        path.write_text(body, encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_parses_thresholds_and_flags(self) -> None:
        from teatree.config import load_config  # noqa: PLC0415

        path = self._write_config(
            "[teatree]\n"
            "disk_crit_free_gb = 5.0\n"
            "ram_crit_avail_gb = 0.5\n"
            "allow_destructive_disk = true\n"
            "ram_kill_allowlist = ['Brave.*Renderer', 'Slack Helper']\n",
        )
        settings = load_config(path).user
        assert settings.disk_crit_free_gb == pytest.approx(5.0)
        assert settings.ram_crit_avail_gb == pytest.approx(0.5)
        assert settings.allow_destructive_disk is True
        assert settings.ram_kill_allowlist == ["Brave.*Renderer", "Slack Helper"]

    def test_explicit_empty_allowlist_is_honoured(self) -> None:
        from teatree.config import load_config  # noqa: PLC0415

        path = self._write_config("[teatree]\ndisk_cache_allowlist = []\n")
        settings = load_config(path).user
        assert settings.disk_cache_allowlist == []

    def test_missing_allowlist_uses_regenerable_default(self) -> None:
        from teatree.config import load_config  # noqa: PLC0415

        path = self._write_config("[teatree]\nloop_cadence_seconds = 60\n")
        settings = load_config(path).user
        assert "~/.cache/pre-commit" in settings.disk_cache_allowlist

    def test_kill_switch_is_overlay_overridable(self) -> None:
        from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS  # noqa: PLC0415

        assert "resource_pressure_disabled" in OVERLAY_OVERRIDABLE_SETTINGS
        assert "allow_destructive_disk" in OVERLAY_OVERRIDABLE_SETTINGS
        assert "disk_crit_free_gb" in OVERLAY_OVERRIDABLE_SETTINGS


class BuilderTests(TestCase):
    """``_resource_pressure_scanner`` honours the kill-switch and threads config."""

    def test_builds_scanner_from_settings(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _resource_pressure_scanner  # noqa: PLC0415

        settings = UserSettings(disk_crit_free_gb=8.0, allow_destructive_disk=True)
        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type("Cfg", (), {"user": settings})(),
        ):
            scanner = _resource_pressure_scanner()
        assert scanner is not None
        assert scanner.disk_crit_free_gb == pytest.approx(8.0)
        assert scanner.allow_destructive_disk is True

    def test_kill_switch_returns_none(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _resource_pressure_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type("Cfg", (), {"user": UserSettings(resource_pressure_disabled=True)})(),
        ):
            assert _resource_pressure_scanner() is None

    def test_build_default_jobs_wires_global_scanner(self) -> None:
        from teatree.loop.global_scanner_factories import build_default_jobs  # noqa: PLC0415
        from teatree.loop.scanners.resource_pressure import ResourcePressureScanner  # noqa: PLC0415

        fake = ResourcePressureScanner()
        with patch("teatree.loop.global_scanner_factories._resource_pressure_scanner", return_value=fake):
            jobs = build_default_jobs()
        assert any(j.scanner is fake and j.overlay == "" for j in jobs)

    def test_build_default_jobs_omits_scanner_when_disabled(self) -> None:
        from teatree.loop.global_scanner_factories import build_default_jobs  # noqa: PLC0415
        from teatree.loop.scanners.resource_pressure import ResourcePressureScanner  # noqa: PLC0415

        with patch("teatree.loop.global_scanner_factories._resource_pressure_scanner", return_value=None):
            jobs = build_default_jobs()
        assert not any(isinstance(j.scanner, ResourcePressureScanner) for j in jobs)
