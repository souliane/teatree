"""Durable history for settings writes through the public manager seam."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import TestCase, override_settings

from teatree.config.db_router import CONFIG_DB_ALIAS, PINNED_MODEL_LABELS
from teatree.config.enums import Autonomy
from teatree.core.models import ConfigSetting, ConfigSettingChange
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from tests._loop_principal_env import pinned_loop_principal


class TestConfigSettingChange(TestCase):
    def test_history_routes_with_setting_to_canonical_database(self) -> None:
        assert ConfigSettingChange._meta.label_lower in PINNED_MODEL_LABELS
        assert ConfigSetting.objects.db == ConfigSettingChange.objects.db
        if CONFIG_DB_ALIAS in connections.databases:
            assert ConfigSettingChange.objects.db == CONFIG_DB_ALIAS

    def test_review_phase_refuses_a_canonical_write(self) -> None:
        database = connections[ConfigSetting.objects.db]
        canonical = Path("/tmp/review-refusal-canonical.sqlite3")
        with (
            patch.dict("os.environ", {"T3_CONFIG_WRITE_PHASE": "reviewing"}),
            patch.dict(database.settings_dict, {"NAME": str(canonical)}),
            patch("teatree.config.write_authority.canonical_config_db", return_value=canonical),
            pytest.raises(ValidationError, match="review or critic"),
        ):
            ConfigSetting.objects.set_value("agent_max_turns", 1000)
        assert ConfigSetting.objects.filter(key="agent_max_turns").exists() is False

    def test_review_phase_can_write_a_throwaway_test_database(self) -> None:
        with patch.dict("os.environ", {"T3_CONFIG_WRITE_PHASE": "reviewing"}):
            ConfigSetting.objects.set_value("agent_max_turns", 1000)
        assert ConfigSetting.objects.get_effective("agent_max_turns") == 1000

    def test_review_phase_refuses_direct_orm_setting_writes(self) -> None:
        database = connections[ConfigSetting.objects.db]
        canonical = Path("/tmp/review-refusal-canonical.sqlite3")
        with (
            override_settings(DATABASE_ROUTERS=["teatree.config.db_router.ConfigSettingRouter"]),
            patch.dict("os.environ", {"T3_CONFIG_WRITE_PHASE": "critic_reviewing"}),
            patch.dict(database.settings_dict, {"NAME": str(canonical)}),
            patch("teatree.config.write_authority.canonical_config_db", return_value=canonical),
            pytest.raises(ValidationError, match="review or critic"),
        ):
            ConfigSetting.objects.create(key="agent_max_turns", value=9)

    def test_review_phase_refuses_bulk_seed_and_clear(self) -> None:
        ConfigSetting.objects.set_value("agent_max_turns", 1000, authorized_by="operator")
        database = connections[ConfigSetting.objects.db]
        canonical = Path("/tmp/review-refusal-canonical.sqlite3")
        with (
            patch.dict("os.environ", {"T3_CONFIG_WRITE_PHASE": "reviewing"}),
            patch.dict(database.settings_dict, {"NAME": str(canonical)}),
            patch("teatree.config.write_authority.canonical_config_db", return_value=canonical),
        ):
            with pytest.raises(ValidationError, match="review or critic"):
                ConfigSetting.objects.set_values([("agent_max_turns", 2000, "")])
            with pytest.raises(ValidationError, match="review or critic"):
                ConfigSetting.objects.seed("agent_max_turns", 2000, code_default=3000)
            with pytest.raises(ValidationError, match="review or critic"):
                ConfigSetting.objects.clear("agent_max_turns")
        assert ConfigSetting.objects.get_effective("agent_max_turns") == 1000
        assert ConfigSettingChange.objects.filter(key="agent_max_turns").count() == 1

    def test_unattended_clear_cannot_remove_a_governed_setting(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", authorized_by="operator")
        with (
            pinned_loop_principal(LOOP_RUNNER_SESSION_ID),
            pytest.raises(ValidationError, match="not an unattended write"),
        ):
            ConfigSetting.objects.clear("autonomy")
        assert ConfigSetting.objects.get_effective("autonomy") == "notify"
        assert ConfigSettingChange.objects.filter(key="autonomy").count() == 1

    def test_set_then_clear_keeps_both_old_values_and_writers(self) -> None:
        ConfigSetting.objects.set_value("agent_max_turns", 1500, authorized_by="operator")
        ConfigSetting.objects.set_value("agent_max_turns", 1000, authorized_by="review-loop")
        assert ConfigSetting.objects.clear("agent_max_turns", authorized_by="operator") is True

        changes = list(ConfigSettingChange.objects.filter(key="agent_max_turns").order_by("changed_at", "pk"))
        assert [(row.action, row.old_value, row.new_value, row.written_by) for row in changes] == [
            ("set", None, 1500, "operator"),
            ("set", 1500, 1000, "review-loop"),
            ("clear", 1000, None, "operator"),
        ]
        assert all(row.changed_at is not None for row in changes)

    def test_same_value_write_does_not_add_an_audit_row(self) -> None:
        ConfigSetting.objects.set_value("agent_max_turns", 1000, authorized_by="operator")
        ConfigSetting.objects.set_value("agent_max_turns", 1000, authorized_by="operator")
        assert ConfigSettingChange.objects.filter(key="agent_max_turns").count() == 1

    def test_same_str_enum_value_does_not_add_an_audit_row(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.NOTIFY, authorized_by="operator")
        ConfigSetting.objects.set_value("autonomy", Autonomy.NOTIFY, authorized_by="operator")
        assert ConfigSettingChange.objects.filter(key="autonomy").count() == 1

    def test_a_write_without_session_identity_names_the_missing_attribution(self) -> None:
        with pinned_loop_principal():
            ConfigSetting.objects.set_value("agent_max_turns", 1000)
        assert ConfigSettingChange.objects.get(key="agent_max_turns").written_by == "unattributed"

    def test_equal_python_values_with_different_json_types_are_written(self) -> None:
        ConfigSetting.objects.set_value("autoload", value=True, authorized_by="operator")
        ConfigSetting.objects.set_value("autoload", 1, authorized_by="operator")
        assert ConfigSettingChange.objects.filter(key="autoload").count() == 2
        assert ConfigSetting.objects.get(key="autoload").value == 1
        ConfigSetting.objects.set_value("subagent_spawn_ceiling", 2, authorized_by="operator")
        ConfigSetting.objects.set_value("subagent_spawn_ceiling", 2.0, authorized_by="operator")
        assert ConfigSettingChange.objects.filter(key="subagent_spawn_ceiling").count() == 2

    def test_manager_rejects_sub_floor_value(self) -> None:
        with pytest.raises(ValidationError, match="agent_max_turns"):
            ConfigSetting.objects.set_value("agent_max_turns", 9)
        with pytest.raises(ValidationError, match="agent_max_turns"):
            ConfigSetting.objects.set_value("agent_max_turns", "9")
        with pytest.raises(ValidationError, match="agent_max_turns"):
            ConfigSetting.objects.set_values([("agent_max_turns", 9, "")])
        with pytest.raises(ValidationError, match="agent_max_turns"):
            ConfigSetting.objects.seed("agent_max_turns", 9, code_default=3000)
        assert not ConfigSetting.objects.filter(key="agent_max_turns").exists()

    def test_same_value_new_writer_and_seed_takeover_are_recorded(self) -> None:
        ConfigSetting.objects.seed("agent_max_turns", 1200, code_default=3000, seeded_by="deploy")
        ConfigSetting.objects.set_value("agent_max_turns", 1200, authorized_by="operator")
        ConfigSetting.objects.set_value("agent_max_turns", 1200, authorized_by="other-operator")
        assert list(
            ConfigSettingChange.objects.filter(key="agent_max_turns").values_list(
                "action", "old_value", "new_value", "written_by"
            )
        ) == [
            ("seed", None, 1200, "deploy"),
            ("set", 1200, 1200, "operator"),
            ("set", 1200, 1200, "other-operator"),
        ]

    def test_seed_update_and_removal_are_recorded(self) -> None:
        ConfigSetting.objects.seed("agent_max_turns", 1400, code_default=3000, seeded_by="deploy")
        ConfigSetting.objects.seed("agent_max_turns", 1500, code_default=3000, seeded_by="deploy")
        ConfigSetting.objects.seed("agent_max_turns", 3000, code_default=3000, seeded_by="deploy")
        assert list(
            ConfigSettingChange.objects.filter(key="agent_max_turns").values_list(
                "action", "old_value", "new_value", "written_by"
            )
        ) == [
            ("seed", None, 1400, "deploy"),
            ("seed", 1400, 1500, "deploy"),
            ("clear", 1500, None, "deploy"),
        ]

    def test_rejected_bulk_write_rolls_back_settings_and_history(self) -> None:
        with pytest.raises(ValidationError):
            ConfigSetting.objects.set_values(
                [("agent_harness", "claude_sdk", ""), ("agent_harness_provider", "openai_compatible", "")]
            )
        assert ConfigSetting.objects.filter(key__in=["agent_harness", "agent_harness_provider"]).exists() is False
        assert ConfigSettingChange.objects.filter(key__in=["agent_harness", "agent_harness_provider"]).exists() is False

    def test_sensitive_values_are_masked_in_history(self) -> None:
        ConfigSetting.objects.set_value("banned_term_registry", {"leak": ["private"]}, authorized_by="operator")
        change = ConfigSettingChange.objects.get(key="banned_term_registry")
        assert change.old_value is None
        assert "private" not in str(change.new_value)
