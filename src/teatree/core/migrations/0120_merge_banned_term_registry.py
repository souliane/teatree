"""Move the four legacy term lists into the classed registry."""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

LEGACY_CLASSES = {
    "banned_brands": "leak",
    "banned_terms": "prose_collider",
    "banned_terms_allowlist": "allow",
    "overlay_leak_terms": "overlay",
}

# A/B/C settings have no reader after this cutover. Explicit names keep this
# migration independent of the current settings schema and decision manifest.
RETIRED_SETTING_KEYS = (
    "adaptive_intake_concurrency_enabled",
    "admin_autologin_enabled",
    "admission_governor_enabled",
    "admission_quota_brake_enabled",
    "agent_signature",
    "allow_destructive_disk",
    "allow_destructive_ram",
    "check_updates",
    "chrome_devtools_headless",
    "chrome_devtools_mcp_enabled",
    "ci_eval_heal_autofix_enabled",
    "claude_chrome",
    "deny_circuit_breaker_enabled",
    "directive_loop_enabled",
    "dispatch_quote_gate_on_task_create_enabled",
    "dispatch_quote_scan_enabled",
    "dream_automation_asks",
    "dream_compliance_escalate",
    "dream_compliance_measure",
    "dream_cross_link",
    "dream_decay",
    "dream_merge",
    "dream_propose_evals",
    "dream_reindex",
    "e2e_confidence_threshold",
    "factory_score_enabled",
    "fleet_claim_enabled",
    "hook_fetch_titles",
    "incremental_push_gate",
    "memory_recall_enabled",
    "notify_on_post_on_behalf",
    "notify_user_via_bot",
    "openai_compatible_sends_prompt_cache_key",
    "orchestrate_claim_enabled",
    "outer_loop_enabled",
    "outer_loop_max_per_week",
    "outer_loop_measure_days",
    "outer_loop_stop_after_consecutive_failures",
    "pull_main_clone_disabled",
    "ram_kill_allowlist",
    "review_request_post_disabled",
    "self_update_disabled",
    "statusline_engaged_render",
    "stop_snapshotter_enabled",
    "task_sweep_disabled",
    "ticket_transition_prune_disabled",
    "worktree_stale_days",
)


def legacy_term_values(key: str, raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(value) for value in raw]
    if isinstance(raw, str):
        return [raw]
    logger.warning(
        "0120: skipped legacy %s value of type %s; it is neither a list nor a string", key, type(raw).__name__
    )
    return []


def merge_legacy_terms(existing: dict | None, legacy: dict) -> dict:
    if existing is not None and not isinstance(existing, dict):
        msg = "banned_term_registry is not a dictionary"
        raise ValueError(msg)
    result = dict(existing) if existing is not None else {}
    for key, term_class in LEGACY_CLASSES.items():
        if key not in legacy:
            continue
        values = legacy_term_values(key, legacy[key])
        current = result.get(term_class, [])
        if not isinstance(current, list) or not all(isinstance(value, str) for value in current):
            msg = f"banned_term_registry.{term_class} must be a list of strings"
            raise ValueError(msg)
        result[term_class] = list(dict.fromkeys([*current, *values]))
    for term_class in ("leak", "prose_collider", "tone", "overlay", "allow"):
        result.setdefault(term_class, [])
    return result


def move_rows(apps, schema_editor) -> None:
    setting = apps.get_model("core", "ConfigSetting")
    rows = setting.objects.using(schema_editor.connection.alias)
    legacy_rows = rows.filter(key__in=LEGACY_CLASSES)
    scopes = legacy_rows.values_list("scope", flat=True).distinct()
    for scope in scopes:
        source = dict(legacy_rows.filter(scope=scope).values_list("key", "value"))
        registry_row = rows.filter(scope=scope, key="banned_term_registry").first()
        merged = merge_legacy_terms(registry_row.value if registry_row else None, source)
        if registry_row is None:
            rows.create(scope=scope, key="banned_term_registry", value=merged)
        else:
            rows.filter(pk=registry_row.pk).update(value=merged)
        legacy_rows.filter(scope=scope).delete()


def retire_settings(apps, schema_editor) -> None:
    setting = apps.get_model("core", "ConfigSetting")
    setting.objects.using(schema_editor.connection.alias).filter(key__in=RETIRED_SETTING_KEYS).delete()


SHIPPED_LOOP_DESCRIPTIONS = {
    "outer_loop": (
        "Advances at most one T4 autoresearch experiment one step per day (propose, ratify, implement, measure, keep-only-if-better), off the live tick; ships disabled behind the outer_loop_enabled flag and the critic-live guard.",
        "Advances at most one T4 autoresearch experiment one step per day (propose, ratify, implement, measure, keep-only-if-better), off the live tick; requires an actively enforced critic merge gate and trustworthy score signals.",
    ),
    "directive_loop": (
        "directive_loop_enabled ships ON, so this Loop row is the remaining switch, and the execution arc additionally needs the factory-score and critic-live guards.",
        "the execution arc additionally needs a live critic and trusted score signals.",
    ),
    "ci_eval_heal": (
        "Advances operator-opened CI-eval heal sessions every 5m (observe-only): dispatch the behavioral eval in CI, poll, and GREEN or HALT+escalate on any red — never a fix. Default-OFF (autonomous CI mutation); an operator opens sessions and enables the row.",
        "Advances operator-opened CI-eval heal sessions every 5m: dispatch the behavioral eval in CI, poll, and fix confirmed reds within the session budget and anti-cheat gate. The loop row controls the cadence.",
    ),
}


def refresh_loop_descriptions(apps, schema_editor) -> None:
    loop = apps.get_model("core", "Loop").objects.using(schema_editor.connection.alias)
    for name, (retired, current) in SHIPPED_LOOP_DESCRIPTIONS.items():
        for row in loop.filter(name=name):
            if retired in row.description:
                loop.filter(pk=row.pk).update(description=row.description.replace(retired, current))


class Migration(migrations.Migration):
    dependencies = [("core", "0119_rename_the_architectural_review_skill")]

    # Reverse is a noop on purpose: pre-0120 code reads the registry first, so the merged
    # union keeps every term verdict; retired rows fall back to their shipped defaults.
    operations = [
        migrations.RunPython(move_rows, migrations.RunPython.noop),
        migrations.RunPython(retire_settings, migrations.RunPython.noop),
        migrations.RunPython(refresh_loop_descriptions, migrations.RunPython.noop),
    ]
