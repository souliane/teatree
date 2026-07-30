# test-path: cross-cutting
"""DB-backup scanner config surface (directive #2) — defaults, override, fail-safe.

The three knobs ship ahead of the Unit-18 scanner that reads them, so this pins
the CONFIG contract the later loop resolves against: the defaults, that a DB row
overrides, and that the "keep a week of backups" cadence/retention bounds FAIL
SAFE to their default on a non-positive value (they can't be mistyped to 0, which
would prune every backup immediately).

Integration-first per the Test-Writing Doctrine: real ``ConfigSetting`` rows
asserted through ``get_effective_settings``.
"""

import pytest
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, get_effective_settings
from teatree.core.models import ConfigSetting


class TestDbBackupDefaults:
    def test_dataclass_defaults(self) -> None:
        settings = UserSettings()
        assert settings.db_backup_disabled is False
        assert settings.db_backup_cadence_hours == 24
        assert settings.db_backup_retention_days == 7

    def test_all_three_keys_are_db_overridable(self) -> None:
        for key in ("db_backup_disabled", "db_backup_cadence_hours", "db_backup_retention_days"):
            assert key in OVERLAY_OVERRIDABLE_SETTINGS


class TestDbBackupResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_empty_store_resolves_to_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.db_backup_disabled is False
        assert settings.db_backup_cadence_hours == 24
        assert settings.db_backup_retention_days == 7

    def test_stored_rows_override(self) -> None:
        ConfigSetting.objects.set_value("db_backup_disabled", value=True)
        ConfigSetting.objects.set_value("db_backup_cadence_hours", 48)
        ConfigSetting.objects.set_value("db_backup_retention_days", 14)
        settings = get_effective_settings()
        assert settings.db_backup_disabled is True
        assert settings.db_backup_cadence_hours == 48
        assert settings.db_backup_retention_days == 14

    def test_non_positive_retention_fails_safe_to_default(self) -> None:
        # A 0 / negative retention would prune every backup immediately — the bound
        # cannot be configured away, so it degrades to the 7-day default.
        ConfigSetting.objects.set_value("db_backup_retention_days", 0)
        assert get_effective_settings().db_backup_retention_days == 7
        ConfigSetting.objects.set_value("db_backup_retention_days", -3)
        assert get_effective_settings().db_backup_retention_days == 7

    def test_non_positive_cadence_fails_safe_to_default(self) -> None:
        ConfigSetting.objects.set_value("db_backup_cadence_hours", 0)
        assert get_effective_settings().db_backup_cadence_hours == 24
