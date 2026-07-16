# test-path: cross-cutting
"""Golden field-set pin for ``UserSettings`` (config §3d #2).

The retired-key guard in ``test_legacy_setting_aliases.py`` re-asserts renames
that are ALREADY recorded, but nothing forces the NEXT rename to be recorded —
the exact #3109 silent-drop class (a stored row under an unrecorded old name is
dropped with no signal). This golden frozenset closes it: ANY change to the
``UserSettings`` field set — an add, a removal, or a rename — turns this test red
with the routing instructions, so recording the rename in
``_RETIRED_SETTING_KEYS`` + ``_LEGACY_SETTING_ALIASES`` (or the removed-dead pin)
can never be forgotten.

The set is maintained by hand ON PURPOSE: that is the whole point — a field-set
change must be a deliberate, reviewed edit here, not an incidental drift.
"""

import dataclasses

from teatree.config import UserSettings

#: Every ``UserSettings`` field name at the current schema. Editing the dataclass
#: WITHOUT updating this set is a red test — see the module docstring for routing.
GOLDEN_USER_SETTINGS_FIELDS: frozenset[str] = frozenset(
    {
        "admin_autologin_enabled",
        "agent_harness",
        "agent_harness_provider",
        "agent_runtime",
        "agent_signature",
        "allow_destructive_disk",
        "allow_destructive_ram",
        "anthropic_api_key_pass_paths",
        "anthropic_oauth_pass_paths",
        "approved_recipe_sha",
        "architectural_review_after_merge_count",
        "architectural_review_cadence_hours",
        "architectural_review_disabled",
        "architectural_review_skill",
        "ask_before_backlog_sweep_closes",
        "ask_before_creating_news_tickets",
        "attachment_gate_enabled",
        "auto_disposition_enabled",
        "auto_disposition_max_closes_per_tick",
        "auto_update_reinstall",
        "auto_update_require_green_main",
        "autoload",
        "autonomy",
        "backlog_sweep_cadence_hours",
        "backlog_sweep_disabled",
        "backlog_sweep_skill",
        "ban_close_trailers_on_namespaces",
        "billing_cycle_anchor_day",
        "boost_concurrency",
        "bulk_close_threshold",
        "check_updates",
        "chrome_devtools_mcp_enabled",
        "claude_chrome",
        "clean_ignore",
        "colleague_repo_url_pattern",
        "contribute",
        "contribute_plugin_dir",
        "critic_gate_mode",
        "db_backup_cadence_hours",
        "db_backup_disabled",
        "db_backup_retention_days",
        "directive_loop_enabled",
        "directive_verify_days",
        "disk_cache_allowlist",
        "disk_crit_free_gb",
        "disk_warn_free_gb",
        "dogfood_smoke_cadence_hours",
        "dogfood_smoke_disabled",
        "dogfood_smoke_overlay",
        "dogfood_smoke_skill",
        "dream_propose_evals",
        "e2e_confidence_threshold",
        "e2e_mandatory_gate_enabled",
        "enforce_regulated_path",
        "eval_credential",
        "eval_local_cadence_hours",
        "eval_local_disabled",
        "eval_local_skill",
        "excluded_skills",
        "factory_score_enabled",
        "fleet_claim_enabled",
        "gate_relaxation_gate_enabled",
        "gitlab_approval_scanner_enabled",
        "handover_mirror_path",
        "hook_fetch_titles",
        "idle_stack_e2e_recent_minutes",
        "idle_stack_idle_minutes",
        "idle_stack_reaper_cadence_minutes",
        "idle_stack_reaper_disabled",
        "incremental_push_gate",
        "issue_implementer_cadence_hours",
        "issue_implementer_enabled",
        "issue_implementer_label",
        "issue_implementer_max_concurrent",
        "issue_implementer_require_label",
        "limit_autorecovery_enabled",
        "local_stack_queue_disabled",
        "local_stack_queue_max_attempts",
        "loop_cadence_seconds",
        "loop_runner_enabled",
        "max_concurrent_local_stacks",
        "max_open_prs_per_repo_per_ticket",
        "max_worktree_gc_per_tick",
        "missing_issue_ref_policy",
        "mode",
        "mr_reminder",
        "mr_title_regex",
        "notify_on_behalf",
        "notify_on_post_on_behalf",
        "notify_user_via_bot",
        "on_behalf_auto_actions",
        "on_behalf_post_mode",
        "orca_router_lane",
        "orca_router_name",
        "orca_router_pass_path",
        "orchestrate_claim_enabled",
        "orchestrator_bash_gate_enabled",
        "outer_loop_enabled",
        "outer_loop_max_per_week",
        "outer_loop_measure_days",
        "outer_loop_stop_after_consecutive_failures",
        "privacy",
        "provision_fast_step_timeout_seconds",
        "provision_max_concurrency",
        "provision_ram_ceiling_percent",
        "provision_slow_threshold_seconds",
        "provision_step_timeout_seconds",
        "pull_main_clone_cadence_hours",
        "pull_main_clone_disabled",
        "pydantic_ai_request_limit",
        "ram_crit_avail_gb",
        "ram_kill_allowlist",
        "ram_warn_avail_gb",
        "regulated_path_model_allowlist",
        "repo_mode",
        "require_anti_vacuity_attestation",
        "require_debt_delta",
        "require_executed_repro",
        "require_human_approval_to_answer",
        "require_human_approval_to_merge",
        "require_integration_review",
        "require_merge_evidence",
        "require_merge_quality_verdict",
        "require_plan_adequacy",
        "require_review_context",
        "require_reviewed_state_for_review_request",
        "require_rubric_verification",
        "require_spec_coverage",
        "resource_pressure_cadence_minutes",
        "resource_pressure_disabled",
        "resource_pressure_min_free_interval_minutes",
        "review_nag_enabled",
        "review_request_dedup_max_pages",
        "review_request_dedup_window_days",
        "review_request_post_disabled",
        "review_skill",
        "scanning_news_cadence_hours",
        "scanning_news_disabled",
        "scanning_news_skill",
        "sdk_monthly_credit_usd",
        "self_update_cadence_hours",
        "self_update_disabled",
        "send_proxy_allowlist",
        "send_proxy_mode",
        "slack_voice_classifier_mode",
        "snapshot_baseline_gate_enabled",
        "snapshot_warmer_disabled",
        "snapshot_warmer_max_age_days",
        "solo_repo_url_pattern",
        "speak",
        "stale_stack_min_age_minutes",
        "statusline_chain",
        "substrate_self_signoff",
        "task_sweep_disabled",
        "task_sweep_recheck_interval_hours",
        "teams_display",
        "teams_enabled",
        "teams_idle_minutes",
        "teams_max_panes",
        "timezone",
        "trusted_issue_authors",
        "user_identity_aliases",
        "wip",
        "workspace_dir",
        "worktree_stale_days",
    }
)

_ROUTING = (
    "The UserSettings field set changed. This is a deliberate-edit gate (config §3d #2):\n"
    "  * ADDED a field   -> add its name to GOLDEN_USER_SETTINGS_FIELDS + register a\n"
    "                       parser in OVERLAY_OVERRIDABLE_SETTINGS (and a reader / a\n"
    "                       conformance-allowlist entry).\n"
    "  * RENAMED a field -> record the old name in resolution._RETIRED_SETTING_KEYS AND\n"
    "                       map it in _LEGACY_SETTING_ALIASES so a stored row is never\n"
    "                       silently dropped (#3109), then update this golden set.\n"
    "  * REMOVED a field -> drop it here AND pin its removal in test_removed_dead_settings.py."
)


def _field_names() -> set[str]:
    return {field.name for field in dataclasses.fields(UserSettings)}


def test_user_settings_field_set_matches_golden() -> None:
    current = _field_names()
    added = sorted(current - GOLDEN_USER_SETTINGS_FIELDS)
    removed = sorted(GOLDEN_USER_SETTINGS_FIELDS - current)
    assert current == GOLDEN_USER_SETTINGS_FIELDS, f"added={added} removed={removed}\n{_ROUTING}"


def test_golden_pin_flags_a_synthetic_add_and_removal() -> None:
    # Anti-vacuity: the exact-match pin fires RED on either a field the golden did
    # not acknowledge (an add) or a golden key the dataclass no longer has (a
    # rename/removal), so the deliberate-edit gate can never be silently vacuous.
    current = _field_names()
    assert current != (GOLDEN_USER_SETTINGS_FIELDS | {"synthetic_added_field"})
    assert current != (GOLDEN_USER_SETTINGS_FIELDS - {"mode"})
