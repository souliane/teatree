"""``generate_settings_defaults`` command — read-only DB → defaults.toml, dry-run.

Integration against the real ``ConfigSetting`` store: a fixture DB carrying a
SECRET row, a SAFETY-posture override, a plain tunable, and a stale key must emit
the empty secret default (absent), keep the fail-closed safety default, adopt the
tunable, and report the stale key — with ``--dry-run`` writing nothing.
"""

import tempfile
import tomllib
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from teatree.config.defaults_generator import Adoption, GenerationReport
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.schema import Category, setting_meta
from teatree.core.management.commands.generate_settings_defaults import Command
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

    def test_command_help_describes_the_regeneration(self) -> None:
        assert "defaults.toml" in Command.help

    def test_without_dry_run_it_writes_the_output_file_and_reports_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "defaults.toml"
            err = StringIO()
            call_command("generate_settings_defaults", "--output", str(out_path), stderr=err)
            assert out_path.exists()
            assert tomllib.loads(out_path.read_text())["teatree"]  # non-empty [teatree] table
            assert f"wrote: {out_path}" in err.getvalue()

    def test_report_surfaces_a_banned_aborted_row(self) -> None:
        # The banned-abort report branch: a synthetic report renders the aborted key + disposition.
        err = StringIO()
        report = GenerationReport(
            banned_aborted=[Adoption("colleague_repo_url_pattern", "x", "x", "banned-abort:brandx")]
        )
        Command(stderr=err)._print_report(report, wrote=None)
        text = err.getvalue()
        assert "banned-term ABORTED (1)" in text
        assert "colleague_repo_url_pattern: banned-abort:brandx" in text
