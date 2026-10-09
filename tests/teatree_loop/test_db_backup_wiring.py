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
    return settings


class DispatchRoutingTests(TestCase):
    def test_due_routes_to_run_db_backup_mechanical(self) -> None:
        signal = ScanSignal(kind="db_backup.due", summary="due", payload={"retention_days": 7})
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "mechanical"
        assert actions[0].zone == "run_db_backup"


class ConfigDefaultsTests(TestCase):
    def test_scanner_ships_a_weeks_retention(self) -> None:
        assert UserSettings().db_backup_retention_days == 7

    def test_the_retention_knob_is_overlay_overridable(self) -> None:
        assert "db_backup_retention_days" in OVERLAY_OVERRIDABLE_SETTINGS


class BuilderTests(TestCase):
    def test_builds_scanner_from_settings(self) -> None:
        settings = UserSettings(db_backup_retention_days=14)
        with patch("teatree.loop.global_scanner_factories.get_effective_settings", return_value=settings):
            scanner = _db_backup_scanner()
        assert scanner is not None
        assert scanner.retention_days == 14

    def test_build_default_jobs_wires_global_scanner(self) -> None:
        fake = DbBackupScanner(retention_days=7)
        with patch("teatree.loop.global_scanner_factories._db_backup_scanner", return_value=fake):
            jobs = build_default_jobs()
        assert any(j.scanner is fake and j.overlay == "" for j in jobs)

    def test_build_default_jobs_omits_scanner_when_disabled(self) -> None:
        with patch("teatree.loop.global_scanner_factories._db_backup_scanner", return_value=None):
            jobs = build_default_jobs()
        assert not any(isinstance(j.scanner, DbBackupScanner) for j in jobs)
