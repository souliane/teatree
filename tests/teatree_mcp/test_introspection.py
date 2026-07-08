"""Config, gate, and command introspection MCP reads."""

from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.mcp import introspection


class TestConfigSettingGet(TestCase):
    def test_db_override_reports_db_source(self) -> None:
        ConfigSetting.objects.set_value("factory_score_enabled", value=True)

        row = introspection.config_setting_get(key="factory_score_enabled")

        assert row["known"] is True
        assert row["value"] is True
        assert row["source"] == "db"
        assert row["scope"] == "global"

    def test_absent_row_falls_through_to_file_env(self) -> None:
        row = introspection.config_setting_get(key="factory_score_enabled")

        assert row["known"] is True
        assert row["source"] == "file/env"
        assert isinstance(row["value"], bool)

    def test_overlay_scope_row_reports_overlay_scope(self) -> None:
        ConfigSetting.objects.set_value("factory_score_enabled", value=True, scope="t3-teatree")

        row = introspection.config_setting_get(key="factory_score_enabled", overlay="t3-teatree")

        assert row["source"] == "db"
        assert row["scope"] == "overlay:t3-teatree"
        assert row["overlay"] == "t3-teatree"

    def test_unknown_key_is_flagged_not_raised(self) -> None:
        row = introspection.config_setting_get(key="not_a_real_setting")

        assert row["known"] is False
        assert row["value"] is None

    def test_path_valued_setting_is_coerced_to_a_string(self) -> None:
        # A Path fallback (workspace_dir) is not JSON-serializable — it must be
        # stringified so the read-only tool never fails at the JSON boundary.
        row = introspection.config_setting_get(key="workspace_dir")

        assert isinstance(row["value"], str)

    def test_list_valued_setting_round_trips_as_a_list(self) -> None:
        row = introspection.config_setting_get(key="excluded_skills")

        assert isinstance(row["value"], list)


class TestJsonable:
    def test_primitives_and_none_pass_through(self) -> None:
        assert introspection._jsonable(None) is None
        assert introspection._jsonable(value=True) is True
        assert introspection._jsonable(3) == 3
        assert introspection._jsonable("x") == "x"

    def test_nested_containers_are_coerced_recursively(self) -> None:
        coerced = introspection._jsonable({"p": Path("/tmp/x"), "nums": [1, 2]})

        assert coerced == {"p": "/tmp/x", "nums": [1, 2]}

    def test_a_non_json_scalar_is_stringified(self) -> None:
        assert introspection._jsonable(object()).startswith("<object object")


class TestGateStatus(TestCase):
    def test_reports_review_and_raw_merge_gate_shape(self) -> None:
        report = introspection.gate_status()

        assert isinstance(report["review_gate"]["require_human_approval_to_merge"], bool)
        assert isinstance(report["raw_merge_gate"]["out_of_band_merge_gate_enabled"], bool)

    def test_review_gate_reflects_a_config_override(self) -> None:
        call_command("config_setting", "set", "require_human_approval_to_merge", "false")

        report = introspection.gate_status()

        assert report["review_gate"]["require_human_approval_to_merge"] is False
