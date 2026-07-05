"""``t3 config show`` — read-only intent + DB-cache view (issue #628).

Integration-first: a real ``~/.teatree.toml`` fixture under a tmp dir with
``teatree.config.CONFIG_PATH`` monkeypatched, exercised through the typer
``CliRunner``. DB-home settings (#1775 — ``mode``, ``contribute``) are staged
through the ``ConfigSetting`` store (their authoritative tier) rather than the
file, so these are DB-backed ``TestCase`` classes. The build step
(``build_config_view``) is unit-tested for the cache-vs-intent partition because
that classification is the load-bearing logic the #628 invariant turns on.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.config_view import build_config_view
from teatree.config import FEATURE_FLAGS
from teatree.core.models import ConfigSetting

runner = CliRunner()


class _TempConfig(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_path = Path(tmp.name) / ".teatree.toml"

    def _write_toml(self, body: str) -> None:
        self.config_path.write_text(body, encoding="utf-8")


class TestBuildConfigView(_TempConfig):
    def test_intent_section_reflects_resolved_settings(self) -> None:
        # mode and contribute are DB-home: the intent (resolved) view reflects
        # the ConfigSetting rows, not a [teatree] TOML value.
        self._write_toml("[teatree]\n")
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("contribute", value=True)
        with patch("teatree.config.CONFIG_PATH", self.config_path):
            view = build_config_view()
        assert view.intent["mode"] == "auto"
        assert view.intent["contribute"] is True
        assert view.config_path == str(self.config_path)
        assert view.config_exists is True

    def test_missing_config_is_reported_not_crashed(self) -> None:
        with patch("teatree.config.CONFIG_PATH", self.config_path.parent / "absent.toml"):
            view = build_config_view()
        assert view.config_exists is False
        # Defaults still resolve so the view is never empty.
        assert view.intent["mode"] == "interactive"

    def test_derived_section_is_labelled_regenerable_cache(self) -> None:
        with patch("teatree.config.CONFIG_PATH", self.config_path.parent / "absent.toml"):
            view = build_config_view()
        # Every derived entry must be flagged regenerable so the #628
        # cache-vs-intent invariant is visible in the output itself.
        assert view.derived  # non-empty
        assert all(entry["regenerable"] is True for entry in view.derived)

    def test_db_path_is_surfaced_in_derived(self) -> None:
        with patch("teatree.config.CONFIG_PATH", self.config_path.parent / "absent.toml"):
            view = build_config_view()
        names = {entry["name"] for entry in view.derived}
        assert "control DB" in names

    def test_feature_flags_are_partitioned_out_of_intent(self) -> None:
        with patch("teatree.config.CONFIG_PATH", self.config_path.parent / "absent.toml"):
            view = build_config_view()
        # A governed feature flag is NOT a durable setting: it must not appear in the
        # user-facing intent dump — it lives in its own stage-labelled flags section.
        for key in FEATURE_FLAGS:
            assert key not in view.intent, f"feature flag {key!r} leaked into the intent dump"
        flag_names = {entry["name"] for entry in view.flags}
        assert set(FEATURE_FLAGS) <= flag_names

    def test_flags_section_carries_stage_and_tracking(self) -> None:
        with patch("teatree.config.CONFIG_PATH", self.config_path.parent / "absent.toml"):
            view = build_config_view()
        by_name = {entry["name"]: entry for entry in view.flags}
        outer = by_name["outer_loop_enabled"]
        assert outer["stage"] == "dark"
        assert outer["tracking_issue"]
        assert outer["value"] is False


class TestConfigShowCommand(_TempConfig):
    def test_human_readable_output_partitions_intent_and_cache(self) -> None:
        self._write_toml("[teatree]\n")
        ConfigSetting.objects.set_value("mode", "auto")
        with patch("teatree.config.CONFIG_PATH", self.config_path):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "Intent" in result.stdout
        assert "source of truth" in result.stdout
        assert "regenerable cache" in result.stdout
        assert "mode" in result.stdout
        assert "auto" in result.stdout

    def test_json_output_is_machine_readable(self) -> None:
        self._write_toml("[teatree]\n")
        ConfigSetting.objects.set_value("mode", "auto")
        with patch("teatree.config.CONFIG_PATH", self.config_path):
            result = runner.invoke(app, ["config", "show", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["intent"]["mode"] == "auto"
        assert payload["config_exists"] is True
        assert isinstance(payload["derived"], list)

    def test_show_is_read_only_no_config_written(self) -> None:
        self._write_toml('[teatree]\nprivacy = "strict"\n')
        before = self.config_path.read_text(encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", self.config_path):
            runner.invoke(app, ["config", "show"])
        assert self.config_path.read_text(encoding="utf-8") == before
