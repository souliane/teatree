# test-path: cross-cutting
"""Golden field-set pin for ``UserSettings`` (config §3d #2).

This golden frozenset makes every change to the ``UserSettings`` field set — an
add, removal, or rename — a deliberate edit. A rename also needs a data migration
to carry stored rows onto the new key.

The set is maintained by hand ON PURPOSE: that is the whole point — a field-set
change must be a deliberate, reviewed edit here, not an incidental drift.
"""

import dataclasses

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings

#: Every ``UserSettings`` field name at the current schema. Editing the dataclass
#: WITHOUT updating this set is a red test — see the module docstring for routing.
GOLDEN_USER_SETTINGS_FIELDS: frozenset[str] = frozenset(
    {
        "admission_pressure_shed_at",
        "admit_colleague_prs_to_board",
        "agent_harness",
        "agent_harness_provider",
        "anthropic_api_key_pass_paths",
        "anthropic_oauth_pass_paths",
        "approved_recipe_sha",
        "architectural_review_after_merge_count",
        "architectural_review_cadence_hours",
        "architectural_review_skill",
        "artifact_idle_days",
        "ask_before_backlog_sweep_closes",
        "ask_before_creating_news_tickets",
        "auto_update_require_green_main",
        "autoload",
        "autonomy",
        "backlog_sweep_skill",
        "ban_close_trailers_on_namespaces",
        "billing_cycle_anchor_day",
        "boost_concurrency",
        "bulk_close_threshold",
        "cheap_phase_admission_ceiling",
        "clean_ignore",
        "colleague_repo_url_pattern",
        "contribute",
        "contribute_plugin_dir",
        "dashboard_instance_label",
        "dashboard_logo",
        "db_backup_retention_days",
        "directive_intake_per_tick",
        "directive_verify_days",
        "disk_cache_allowlist",
        "disk_crit_free_gb",
        "disk_warn_free_gb",
        "dogfood_smoke_overlay",
        "dogfood_smoke_skill",
        "drain_slot_reservation",
        "dream_memory_promote",
        "dream_umbrella_url",
        "envelope_stop_gate_refusals",
        "eval_local_skill",
        "excluded_skills",
        "expected_required_contexts",
        "gate_relaxation_gate_enabled",
        "gitlab_events_subscription",
        "handover_mirror_path",
        "harness_skill_exclusions",
        "agent_max_turns",
        "idle_stack_e2e_recent_minutes",
        "idle_stack_idle_minutes",
        "independent_reviewer_identities",
        "intake_ram_per_agent_gb",
        "intake_ram_reserve_gb",
        "issue_implementer_label",
        "issue_implementer_max_concurrent",
        "issue_intake_pass_budget_seconds",
        "loop_cadence_seconds",
        "max_concurrent_local_stacks",
        "max_open_prs_per_repo_per_ticket",
        "merge_wip",
        "metered_spend_window_hours",
        "metered_token_ceiling",
        "mode",
        "mr_reminder",
        "mr_title_regex",
        "notion_write_allowed_roots",
        "notion_write_denied_roots",
        "on_behalf_auto_actions",
        "openai_compatible_base_url",
        "openai_compatible_credential_entry",
        "openai_compatible_extra_headers",
        "openai_compatible_lane",
        "openai_compatible_model",
        "orchestrator_bash_gate_enabled",
        "orphan_group_min_age_hours",
        "pr_review_backend",
        "provision_fast_step_timeout_seconds",
        "provision_max_concurrency",
        "provision_ram_ceiling_percent",
        "provision_slow_threshold_seconds",
        "provision_step_timeout_seconds",
        "pull_main_clone_cadence_hours",
        "pydantic_ai_max_tokens",
        "pydantic_ai_request_limit",
        "ram_crit_avail_gb",
        "ram_warn_avail_gb",
        "regulated_path_model_allowlist",
        "repo_mode",
        "require_human_approval_to_answer",
        "require_human_approval_to_merge",
        "review_backend_cooldown_hours",
        "review_exempt_repos",
        "review_exempt_repos_count_toward_group_readiness",
        "review_nag_max_interval_days",
        "review_nag_reask_mention",
        "review_skill",
        "review_skill_alternates",
        "scanner_overlay_scope",
        "schema_readiness_gate_enabled",
        "scanning_news_cadence_hours",
        "scanning_news_skill",
        "scratch_retention_days",
        "scratch_sweep_root",
        "sdk_monthly_credit_usd",
        "send_proxy_allowlist",
        "session_stale_after_hours",
        "single_branch_repos",
        "snapshot_baseline_gate_enabled",
        "snapshot_warmer_max_age_days",
        "solo_repo_url_pattern",
        "speak",
        "stale_stack_min_age_minutes",
        "statusline_chain",
        "subagent_spawn_ceiling",
        "substrate_auto_merge_authorized_by",
        "substrate_self_signoff",
        "target_branch",
        "task_attempt_retention_days",
        "task_result_retention_days",
        "task_sweep_recheck_interval_hours",
        "test_worker_ram_gb",
        "ticket_budget_max_cost_usd",
        "trusted_issue_authors",
        "umbrella_issue_labels",
        "user_identity_aliases",
        "watchdog_max_cost_usd",
        "watchdog_max_runtime_seconds",
        "watchdog_max_turns",
        "wip",
        "worker_quiescing",
        "write_wip",
        "workspace_dir",
    }
)

_ROUTING = (
    "The UserSettings field set changed. This is a deliberate-edit gate (config §3d #2):\n"
    "  * ADDED a field   -> add its name to GOLDEN_USER_SETTINGS_FIELDS + register a\n"
    "                       parser in OVERLAY_OVERRIDABLE_SETTINGS (and a reader / a\n"
    "                       conformance-allowlist entry).\n"
    "  * RENAMED a field -> migrate stored ConfigSetting rows, then update this golden set.\n"
    "  * REMOVED a field -> drop it here; test_setting_decisions.py pins audited removals."
)


def _field_names() -> set[str]:
    return {field.name for field in dataclasses.fields(UserSettings)}


def test_user_settings_field_set_matches_golden() -> None:
    current = _field_names()
    added = sorted(current - GOLDEN_USER_SETTINGS_FIELDS)
    removed = sorted(GOLDEN_USER_SETTINGS_FIELDS - current)
    assert current == GOLDEN_USER_SETTINGS_FIELDS, f"added={added} removed={removed}\n{_ROUTING}"


def test_every_db_home_setting_has_a_user_settings_field() -> None:
    assert set(OVERLAY_OVERRIDABLE_SETTINGS) <= GOLDEN_USER_SETTINGS_FIELDS


def test_golden_pin_flags_a_synthetic_add_and_removal() -> None:
    # Anti-vacuity: the exact-match pin fires RED on either a field the golden did
    # not acknowledge (an add) or a golden key the dataclass no longer has (a
    # rename/removal), so the deliberate-edit gate can never be silently vacuous.
    current = _field_names()
    assert current != (GOLDEN_USER_SETTINGS_FIELDS | {"synthetic_added_field"})
    assert current != (GOLDEN_USER_SETTINGS_FIELDS - {"mode"})
