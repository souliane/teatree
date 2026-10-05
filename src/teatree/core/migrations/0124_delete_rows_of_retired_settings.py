"""Retire settings; reverses lose data; only rollback restores pre-roll SQLite .backup with previous image."""

import logging

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

logger = logging.getLogger(__name__)

KEYS = (
    "adaptive_intake_concurrency_enabled",
    "admin_autologin_enabled",
    "admission_governor_enabled",
    "admission_quota_brake_enabled",
    "agent_phase_fanout",
    "agent_signature",
    "allow_destructive_disk",
    "allow_destructive_ram",
    "architectural_review_disabled",
    "architectural_review_retry_backoff_hours",
    "ask_before_post_on_behalf",
    "attachment_gate_enabled",
    "auto_disposition_enabled",
    "auto_disposition_max_closes_per_tick",
    "auto_update_reinstall",
    "availability_schedule",
    "backlog_sweep_cadence_hours",
    "backlog_sweep_disabled",
    "banned_terms_required",
    "brief_anchor_gate_refuse",
    "branch_prefix",
    "check_updates",
    "chrome_devtools_headless",
    "chrome_devtools_mcp_enabled",
    "ci_eval_heal_autofix_enabled",
    "claude_chrome",
    "critic_gate_mode",
    "db_backup_cadence_hours",
    "db_backup_disabled",
    "deferred_question_age_ceiling_days",
    "deferred_question_max_escalations",
    "deny_circuit_breaker_enabled",
    "deny_circuit_breaker_threshold",
    "directive_loop_enabled",
    "dispatch_quote_gate_on_task_create_enabled",
    "dispatch_quote_scan_enabled",
    "dogfood_smoke_cadence_hours",
    "dogfood_smoke_disabled",
    "dream_automation_asks",
    "dream_compliance_escalate",
    "dream_compliance_measure",
    "dream_cross_link",
    "dream_decay",
    "dream_derive_evals",
    "dream_merge",
    "dream_promotion_cap",
    "dream_propose_evals",
    "dream_reindex",
    "dream_validate_live",
    "e2e_mandatory_gate_enabled",
    "e2e_confidence_threshold",
    "eval_credential",
    "eval_local_cadence_hours",
    "eval_local_disabled",
    "enforce_regulated_path",
    "factory_score_enabled",
    "fleet_claim_enabled",
    "gitlab_approval_scanner_enabled",
    "headless_max_turns",
    "hook_fetch_titles",
    "idle_stack_reaper_cadence_minutes",
    "idle_stack_reaper_disabled",
    "incoming_event_retention_days",
    "internal_publish_namespaces",
    "incremental_push_gate",
    "issue_implementer_cadence_hours",
    "issue_implementer_enabled",
    "issue_implementer_require_label",
    "limit_autorecovery_enabled",
    "local_stack_queue_disabled",
    "local_stack_queue_max_attempts",
    "loop_runner_enabled",
    "low_power_auto_engage",
    "low_power_preset_name",
    "max_worktree_gc_per_tick",
    "memory_recall_enabled",
    "mcp_privacy_gate_enabled",
    "merged_detection_gate_enabled",
    "missing_issue_ref_policy",
    "mr_conflict_scan_enabled",
    "mr_state_questions_max_per_tick",
    "mr_triage_enabled",
    "mr_triage_max_mrs_per_tick",
    "notify_on_post_on_behalf",
    "notify_user_via_bot",
    "on_behalf_post_mode",
    "openai_compatible_sends_prompt_cache_key",
    "orca_router_lane",
    "orca_router_name",
    "orca_router_pass_path",
    "orchestrator_investigation_gate_enabled",
    "orchestrate_claim_enabled",
    "outer_loop_enabled",
    "outer_loop_max_per_week",
    "outer_loop_measure_days",
    "outer_loop_stop_after_consecutive_failures",
    "park_attempt_retention_days",
    "privacy",
    "pull_main_clone_disabled",
    "ram_kill_allowlist",
    "require_anti_vacuity_attestation",
    "require_debt_delta",
    "require_executed_repro",
    "require_integration_review",
    "require_merge_evidence",
    "require_merge_quality_verdict",
    "require_plan_adequacy",
    "require_review_context",
    "require_reviewed_state_for_review_request",
    "require_rubric_verification",
    "require_spec_coverage",
    "require_work_group_batch",
    "resource_pressure_cadence_minutes",
    "resource_pressure_disabled",
    "resource_pressure_min_free_interval_minutes",
    "review_nag_enabled",
    "review_pause_reaction_emojis",
    "review_request_dedup_max_pages",
    "review_request_dedup_window_days",
    "review_request_post_disabled",
    "review_resume_reply_enabled",
    "scanning_news_disabled",
    "send_proxy_mode",
    "self_update_cadence_hours",
    "self_update_disabled",
    "snapshot_warmer_disabled",
    "slack_voice_classifier_mode",
    "statusline_engaged_render",
    "stop_snapshotter_enabled",
    "task_sweep_disabled",
    "teams_display",
    "teams_enabled",
    "teams_idle_minutes",
    "teams_max_panes",
    "ticket_transition_prune_disabled",
    "timezone",
    "todo_sweep_disabled",
    "todo_sweep_recheck_interval_hours",
    "token_outage_auto_engage",
    "triage_assessor_cadence_hours",
    "triage_assessor_enabled",
    "triage_assessor_max_issues_per_tick",
    "venv_idle_days",
    "work_group_generic_scopes",
    "work_group_max_members",
    "worktree_occupancy_gate_enabled",
    "worktree_occupancy_lease_seconds",
    "worktree_stale_days",
    "worktrees_dir",
)


def _canonical_private_repo(entry: object) -> str | None:
    if not isinstance(entry, str):
        return None
    canonical = entry.rstrip("/").lower()
    canonical = canonical.removesuffix(".git") if "/" in canonical else canonical
    parts = canonical.split("/")
    if (
        "/" not in canonical
        or "." not in parts[0]
        or any(not part or part in {".", ".."} for part in parts)
        or any(char.isspace() or char in "*?[]{}\\:@#%" for char in canonical)
    ):
        return None
    return canonical


def _rename_sweep_interval(rows) -> None:
    old_key = "todo_sweep_recheck_interval_hours"
    new_key = "task_sweep_recheck_interval_hours"
    for pk, scope in rows.filter(key=old_key).values_list("pk", "scope").iterator():
        if not rows.filter(scope=scope, key=new_key).exists():
            rows.filter(pk=pk).update(key=new_key)


def carry_retired_values(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Carry retired private namespaces into the global allowlist."""
    config_setting = apps.get_model("core", "ConfigSetting")
    rows = config_setting.objects.using(schema_editor.connection.alias)
    _rename_sweep_interval(rows)

    dropped = 0
    preserved = 0
    target = rows.filter(scope="", key="private_repos").values_list("pk", "value").first()
    blocked = target is not None and not isinstance(target[1], list)
    current = target[1] if target is not None and not blocked else []
    merged = list(current)
    seen = {_canonical_private_repo(entry) for entry in current}
    for value in (
        rows.filter(key="internal_publish_namespaces").order_by("pk").values_list("value", flat=True).iterator()
    ):
        if blocked:
            preserved += 1
        if not isinstance(value, list):
            dropped += 1
            continue
        for entry in value:
            canonical = _canonical_private_repo(entry)
            if canonical is None:
                dropped += 1
            elif not blocked and canonical not in seen:
                merged.append(canonical)
                seen.add(canonical)
    if target is not None and not blocked and merged != current:
        rows.filter(pk=target[0]).update(value=merged)
    elif target is None and merged:
        rows.create(scope="", key="private_repos", value=merged)
    logger.info(
        "0124: %s invalid internal namespace entry/row(s) left unresolved; %s source row(s) blocked by non-list global private_repos",
        dropped,
        preserved,
    )


def delete_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting")
    rows = config_setting.objects.using(schema_editor.connection.alias)
    blocked = rows.filter(scope="", key="private_repos").values_list("value", flat=True).first()
    blocked = blocked is not None and not isinstance(blocked, list)
    for pk, value in rows.filter(key="internal_publish_namespaces").values_list("pk", "value").iterator():
        if blocked or not isinstance(value, list) or any(_canonical_private_repo(entry) is None for entry in value):
            continue
        rows.filter(pk=pk).delete()
    rows.filter(key__in=tuple(key for key in KEYS if key != "internal_publish_namespaces")).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0123_merge_private_and_public_leaves")]

    operations = [
        migrations.RunPython(carry_retired_values, migrations.RunPython.noop),
        migrations.RunPython(delete_rows, migrations.RunPython.noop),
    ]
