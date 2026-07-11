"""Wiring tests for the DB-backup scanner (directive #2).

Covers the dispatch routing (``db_backup.due`` → the mechanical handler), the
``_db_backup_scanner`` factory (config threading + kill-switch), and the
``build_default_jobs`` global registration.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings
from teatree.loop.dispatch import dispatch
from teatree.loop.global_scanner_factories import _db_backup_scanner, build_default_jobs
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.db_backup import DbBackupScanner


def _cfg(settings: UserSettings) -> object:
    return type("Cfg", (), {"user": settings})()


class DispatchRoutingTests(TestCase):
    def test_due_routes_to_run_db_backup_mechanical(self) -> None:
        signal = ScanSignal(kind="db_backup.due", summary="due", payload={"retention_days": 7})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "mechanical"
        assert actions[0].zone == "run_db_backup"


class ConfigDefaultsTests(TestCase):
    def test_scanner_enabled_by_default(self) -> None:
        settings = UserSettings()
        assert settings.db_backup_disabled is False
        assert settings.db_backup_cadence_hours == 24
        assert settings.db_backup_retention_days == 7

    def test_knobs_are_overlay_overridable(self) -> None:
        for key in ("db_backup_disabled", "db_backup_cadence_hours", "db_backup_retention_days"):
            assert key in OVERLAY_OVERRIDABLE_SETTINGS


class BuilderTests(TestCase):
    def test_builds_scanner_from_settings(self) -> None:
        settings = UserSettings(db_backup_cadence_hours=48, db_backup_retention_days=14)
        with patch("teatree.loop.global_scanner_factories.load_config", return_value=_cfg(settings)):
            scanner = _db_backup_scanner()
        assert scanner is not None
        assert scanner.cadence_hours == 48
        assert scanner.retention_days == 14

    def test_kill_switch_returns_none(self) -> None:
        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=_cfg(UserSettings(db_backup_disabled=True)),
        ):
            assert _db_backup_scanner() is None

    def test_build_default_jobs_wires_global_scanner(self) -> None:
        fake = DbBackupScanner(retention_days=7)
        with patch("teatree.loop.global_scanner_factories._db_backup_scanner", return_value=fake):
            jobs = build_default_jobs()
        assert any(j.scanner is fake and j.overlay == "" for j in jobs)

    def test_build_default_jobs_omits_scanner_when_disabled(self) -> None:
        with patch("teatree.loop.global_scanner_factories._db_backup_scanner", return_value=None):
            jobs = build_default_jobs()
        assert not any(isinstance(j.scanner, DbBackupScanner) for j in jobs)
