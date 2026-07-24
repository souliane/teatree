"""``generate_settings_defaults`` command — read-only DB → defaults.toml, dry-run.

Integration against the real ``ConfigSetting`` store: a fixture DB carrying a
SECRET row, a SAFETY-posture override, a plain tunable, and a stale key must emit
the empty secret default (absent), keep the fail-closed safety default, adopt the
tunable, and report the stale key — with ``--dry-run`` writing nothing.
"""

import tomllib
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.schema import Category, setting_meta
from teatree.core.models import ConfigSetting


class TestGenerateSettingsDefaultsCommand(TestCase):
    def _run_dry(self) -> tuple[dict, str]:
        out, err = StringIO(), StringIO()
        call_command("generate_settings_defaults", "--dry-run", stdout=out, stderr=err)
        return tomllib.loads(out.getvalue())["teatree"], err.getvalue()

    def test_fixture_db_emits_safe_defaults_and_reports_the_rest(self) -> None:
        ConfigSetting.objects.set_value("banned_terms", ["acme-bank"])  # SECRET
        ConfigSetting.objects.set_value("autonomy", "full")  # SAFETY-posture
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 70)  # plain tunable
        ConfigSetting.objects.set_value("some_retired_key", "leftover")  # stale/unknown

        emitted, report = self._run_dry()

        assert "banned_terms" not in emitted  # SECRET → empty code default, never shipped
        assert emitted["autonomy"] == "babysit"  # fail-closed default kept, not the live "full"
        assert emitted["provision_ram_ceiling_percent"] == 70  # tunable adopted from live
        assert "some_retired_key" in report  # stale key surfaced for the operator

    def test_overlay_scope_rows_are_reported_never_emitted(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="my-overlay")
        emitted, report = self._run_dry()
        assert emitted["autonomy"] == "babysit"
        assert "[my-overlay] autonomy" in report

    def test_dry_run_writes_nothing_and_emits_only_default_keys(self) -> None:
        emitted, _ = self._run_dry()
        assert set(emitted) == {k for k in ALL_KNOWN_CONFIG_SETTINGS if setting_meta(k).category is Category.DEFAULT}
