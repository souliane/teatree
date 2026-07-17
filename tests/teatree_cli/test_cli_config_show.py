"""``t3 config show`` — read-only intent + DB-cache view (issue #628).

Integration-first, exercised through the typer ``CliRunner``. The intent section
is the resolved DB-home ``ConfigSetting`` store (its authoritative tier), so
these are DB-backed ``TestCase`` classes seeding via ``ConfigSetting.objects``.
The build step (``build_config_view``) is unit-tested for the cache-vs-intent
partition because that classification is the load-bearing logic the #628
invariant turns on.
"""

import json

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.config_view import build_config_view
from teatree.config import FEATURE_FLAGS
from teatree.core.models import ConfigSetting

runner = CliRunner()


class TestBuildConfigView(TestCase):
    def test_intent_section_reflects_resolved_settings(self) -> None:
        # mode and contribute are DB-home: the intent (resolved) view reflects
        # the ConfigSetting rows.
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("contribute", value=True)
        view = build_config_view()
        assert view.intent["mode"] == "auto"
        assert view.intent["contribute"] is True

    def test_defaults_resolve_when_no_overrides(self) -> None:
        view = build_config_view()
        # Defaults still resolve so the view is never empty.
        assert view.intent["mode"] == "interactive"

    def test_derived_section_labels_regenerability_per_entry(self) -> None:
        view = build_config_view()
        by_name = {entry["name"]: entry for entry in view.derived}
        # The canonical control DB (and its row-count view) hold durable user
        # intent — tickets, sessions, merge approvals, ConfigSetting rows — so
        # they must NOT be flagged regenerable/deletable (#3313). The data-dir
        # cache files remain regenerable so the #628 invariant stays visible.
        assert by_name["control DB"]["regenerable"] is False
        if "DB row counts" in by_name:
            assert by_name["DB row counts"]["regenerable"] is False
        cache_entries = [e for e in view.derived if e["name"] not in {"control DB", "DB row counts"}]
        assert cache_entries  # the regenerable data-dir cache files
        assert all(entry["regenerable"] is True for entry in cache_entries)

    def test_control_db_rendered_as_not_regenerable(self) -> None:
        from teatree.cli.config_view import render_config_view  # noqa: PLC0415

        rendered = render_config_view(build_config_view())
        control_line = next(line for line in rendered.splitlines() if "control DB" in line)
        assert "NOT regenerable" in control_line
        assert "regenerable cache" not in control_line

    def test_db_path_is_surfaced_in_derived(self) -> None:
        view = build_config_view()
        names = {entry["name"] for entry in view.derived}
        assert "control DB" in names

    def test_feature_flags_are_partitioned_out_of_intent(self) -> None:
        view = build_config_view()
        # A governed feature flag is NOT a durable setting: it must not appear in the
        # user-facing intent dump — it lives in its own stage-labelled flags section.
        for key in FEATURE_FLAGS:
            assert key not in view.intent, f"feature flag {key!r} leaked into the intent dump"
        flag_names = {entry["name"] for entry in view.flags}
        assert set(FEATURE_FLAGS) <= flag_names

    def test_flags_section_carries_stage_and_tracking(self) -> None:
        view = build_config_view()
        by_name = {entry["name"]: entry for entry in view.flags}
        outer = by_name["outer_loop_enabled"]
        assert outer["stage"] == "dark"
        assert outer["tracking_issue"]
        assert outer["value"] is False


class TestConfigShowCommand(TestCase):
    def test_human_readable_output_partitions_intent_and_cache(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "Intent" in result.stdout
        assert "source of truth" in result.stdout
        assert "regenerable cache" in result.stdout
        assert "mode" in result.stdout
        assert "auto" in result.stdout

    def test_json_output_is_machine_readable(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        result = runner.invoke(app, ["config", "show", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["intent"]["mode"] == "auto"
        assert isinstance(payload["derived"], list)

    def test_show_is_read_only_no_config_written(self) -> None:
        ConfigSetting.objects.set_value("privacy", "strict")
        runner.invoke(app, ["config", "show"])
        assert ConfigSetting.objects.get_effective("privacy") == "strict"
