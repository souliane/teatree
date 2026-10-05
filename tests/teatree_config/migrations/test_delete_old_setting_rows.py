"""The one-time cleanup deletes only rows named by migration 0124."""

from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS

_BEFORE = ("core", "0123_merge_private_and_public_leaves")
_AFTER = ("core", next(Path(__file__).parents[3].glob("src/teatree/core/migrations/0124_*.py")).stem)
_ALWAYS_ON_GATE_KEYS = (
    "critic_gate_mode",
    "dream_derive_evals",
    "dream_validate_live",
    "require_anti_vacuity_attestation",
    "require_debt_delta",
    "require_executed_repro",
    "require_integration_review",
    "require_merge_evidence",
    "require_merge_quality_verdict",
    "require_review_context",
    "require_reviewed_state_for_review_request",
    "require_work_group_batch",
    "token_outage_auto_engage",
)
_ADDITIONAL_RETIRED_KEYS = (
    "agent_phase_fanout",
    "attachment_gate_enabled",
    "banned_terms_required",
    "brief_anchor_gate_refuse",
    "e2e_mandatory_gate_enabled",
    "enforce_regulated_path",
    "mcp_privacy_gate_enabled",
    "merged_detection_gate_enabled",
    "missing_issue_ref_policy",
    "orchestrator_investigation_gate_enabled",
    "send_proxy_mode",
    "slack_voice_classifier_mode",
    "worktree_occupancy_gate_enabled",
)


def test_additional_retired_keys_are_not_live_registry_keys() -> None:
    assert set(_ADDITIONAL_RETIRED_KEYS).isdisjoint(ALL_KNOWN_CONFIG_SETTINGS)


@pytest.mark.timeout(240)
class TestDeleteOldSettingRows(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_listed_rows_and_all_gate_scopes_are_deleted_without_touching_live_settings(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        setting = executor.loader.project_state([_BEFORE]).apps.get_model("core", "ConfigSetting")
        setting.objects.bulk_create(
            [
                setting(scope="", key="speed", value="slow"),
                setting(scope="", key="self_update_disabled", value=True),
                setting(scope="", key="wip", value="full"),
                setting(scope="", key="dream_memory_promote", value=True),
                setting(scope="", key="todo_sweep_recheck_interval_hours", value=24),
                setting(scope="t3-acme", key="todo_sweep_recheck_interval_hours", value=48),
                setting(scope="t3-acme", key="task_sweep_recheck_interval_hours", value=12),
                setting(scope="", key="internal_publish_namespaces", value=["gitlab.com/group/repo"]),
                setting(
                    scope="t3-acme",
                    key="internal_publish_namespaces",
                    value=["github.com/org/repo", "gitlab.com/group/another"],
                ),
                setting(scope="t3-acme", key="private_repos", value=["github.com/org/repo"]),
                *[
                    setting(scope=scope, key=key, value=scope != "")
                    for key in (*_ALWAYS_ON_GATE_KEYS, *_ADDITIONAL_RETIRED_KEYS)
                    for scope in ("", "t3-acme")
                ],
            ]
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT value FROM teatree_config_setting WHERE scope = %s AND key = %s",
                ["t3-acme", "private_repos"],
            )
            row = cursor.fetchone()
            assert row is not None
            scoped_private_repos_raw = row[0]

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])
        setting = executor.loader.project_state([_AFTER]).apps.get_model("core", "ConfigSetting")

        assert list(setting.objects.order_by("scope", "key").values_list("scope", "key", "value")) == [
            ("", "dream_memory_promote", True),
            ("", "private_repos", ["gitlab.com/group/repo", "github.com/org/repo", "gitlab.com/group/another"]),
            ("", "speed", "slow"),
            ("", "task_sweep_recheck_interval_hours", 24),
            ("", "wip", "full"),
            ("t3-acme", "private_repos", ["github.com/org/repo"]),
            ("t3-acme", "task_sweep_recheck_interval_hours", 12),
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT value FROM teatree_config_setting WHERE scope = %s AND key = %s",
                ["t3-acme", "private_repos"],
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == scoped_private_repos_raw
