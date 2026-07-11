# test-path: cross-cutting
"""The retired-key alias contract for DB-home settings (souliane/teatree#3109).

A DB-home ``UserSettings`` field renamed without an alias (or a data migration)
silently discards any ``ConfigSetting`` row stored under the old key: the row
falls through ``_coerce_db_rows`` and the field takes its dataclass default. The
``speed`` -> ``wip`` rename (#2951) was exactly that — an install that set
``speed = full`` silently ran at ``Wip.MEDIUM``.

Two guards live here:

*   the instance guard — a stored ``speed`` row resolves to ``Wip.FULL`` through
    the ``speed -> wip`` alias;
*   the class-of-bug guard — every key that has ever been a DB-home settings
    field name (the explicitly-maintained ``_RETIRED_SETTING_KEYS`` registry) is
    still reachable: a current ``UserSettings`` field or a ``_LEGACY_SETTING_ALIASES``
    entry, so the next rename cannot silently drop an operator's setting.

Integration-first per the Test-Writing Doctrine: real ``ConfigSetting`` rows
resolved through ``get_effective_settings``.
"""

import dataclasses

import pytest
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, Autonomy, UserSettings, Wip, get_effective_settings
from teatree.config import resolution as _resolution
from teatree.config.resolution import _LEGACY_SETTING_ALIASES, _RETIRED_SETTING_KEYS
from teatree.core.models import ConfigSetting

# The append-only GOLDEN snapshot of every DB-home settings key (the keys of
# ``OVERLAY_OVERRIDABLE_SETTINGS``). It only GROWS: a new field is added here, and a
# renamed/removed field is LEFT here and recorded in ``_RETIRED_SETTING_KEYS`` (renamed,
# aliased) or ``_REMOVED_DEAD_SETTING_KEYS`` (removed-dead). This makes the previously
# opt-in retired-key discipline mechanical (U10/3d#2): the guard below fails the moment
# the live field set diverges from this pin without the retirement recorded, so a rename
# can never silently orphan a stored ``ConfigSetting`` row. Pinned HERE (not in a
# ``src`` settings-resolution module) so its field-name string literals are not
# mis-counted as settings reads by ``test_user_settings_readers``' AST reader walk.
_GOLDEN_SETTING_FIELDS: frozenset[str] = frozenset(
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
        "task_sweep_disabled",
        "task_sweep_recheck_interval_hours",
        "teams_display",
        "teams_enabled",
        "teams_idle_minutes",
        "teams_max_panes",
        "timezone",
        "user_identity_aliases",
        "wip",
        "workspace_dir",
        "worktree_stale_days",
    }
)

# DB-home keys removed as provably-dead — the field intentionally resolves to nothing
# (NOT renamed, so NOT aliased). Paired with ``_RETIRED_SETTING_KEYS`` as the accounting
# buckets the golden guard checks: a golden key no longer live must sit in exactly one
# bucket. Guarded field-by-field in ``tests/config/test_removed_dead_settings.py``.
_REMOVED_DEAD_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "branch_prefix",
        "ask_before_post_on_behalf",
        "worktrees_dir",
    }
)


class RetiredSpeedRowResolvesToWip(TestCase):
    """A ``ConfigSetting`` row under the retired ``speed`` key still takes effect."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_WIP", raising=False)

    def test_stored_speed_full_resolves_to_wip_full(self) -> None:
        # The bug: on main this resolves Wip.MEDIUM because `speed` has no alias.
        ConfigSetting.objects.set_value("speed", "full")
        assert get_effective_settings().wip is Wip.FULL

    def test_stored_speed_slow_resolves_to_wip_slow(self) -> None:
        ConfigSetting.objects.set_value("speed", "slow")
        assert get_effective_settings().wip is Wip.SLOW

    def test_canonical_wip_row_wins_over_retired_speed_row(self) -> None:
        # The alias only fills a gap: a current `wip` row always beats a `speed` row.
        ConfigSetting.objects.set_value("speed", "boost")
        ConfigSetting.objects.set_value("wip", "slow")
        assert get_effective_settings().wip is Wip.SLOW


class AliasFoldedGatePinSurvivesAutonomyCollapse(TestCase):
    """A global approval-gate row stored under a RETIRED alias still pins the gate (config §3d #1).

    ``get_effective_settings`` folds a legacy-alias row's VALUE onto its current
    field via ``_coerce_db_rows`` — but the autonomy-collapse pin set must be keyed
    off those FOLDED field names, not the raw row keys. On the buggy code the pin
    set was ``set(global_rows)`` (raw keys), so an approval gate stored under a
    renamed key resolved its value while its pin vanished: the ``full``/``notify``
    collapse then overrode the operator's explicitly-stored gate. This is the
    RED-at-HEAD proof; the fix keys the pin set off ``global_db`` (folded names).
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_WIP"):
            monkeypatch.delenv(env, raising=False)
        # Simulate a future rename of an approval gate: the row is stored under the
        # old key and folded onto the current field on read.
        monkeypatch.setattr(
            _resolution,
            "_LEGACY_SETTING_ALIASES",
            {**_LEGACY_SETTING_ALIASES, "legacy_merge_gate": "require_human_approval_to_merge"},
        )

    def test_aliased_global_gate_row_survives_full_autonomy(self) -> None:
        # Operator explicitly kept the merge gate ON, but under the OLD key name;
        # the full-autonomy collapse must NOT relax it back to False.
        ConfigSetting.objects.set_value("autonomy", "full")
        ConfigSetting.objects.set_value("legacy_merge_gate", value=True)
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.FULL
        assert settings.require_human_approval_to_merge is True

    def test_canonical_row_under_new_key_also_pins(self) -> None:
        # The non-aliased control: a row under the current key pins identically.
        ConfigSetting.objects.set_value("autonomy", "full")
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=True)
        assert get_effective_settings().require_human_approval_to_merge is True


class RetiredSettingKeysStayReachable(TestCase):
    """The class-of-bug guard: a renamed DB-home key must never silently drop rows.

    ``_RETIRED_SETTING_KEYS`` is the explicitly-maintained record of every key that
    has ever been a DB-home settings field name. Retiring a key is a deliberate
    two-part edit (record it here, wire its alias), and this guard fails loudly if
    the alias half is missing.
    """

    @staticmethod
    def _current_fields() -> set[str]:
        return {field.name for field in dataclasses.fields(UserSettings)}

    def test_every_retired_key_is_a_current_field_or_aliased(self) -> None:
        current = self._current_fields()
        unresolved = sorted(
            key for key in _RETIRED_SETTING_KEYS if key not in current and key not in _LEGACY_SETTING_ALIASES
        )
        assert not unresolved, (
            f"Retired DB-home settings key(s) {unresolved} are neither a current UserSettings "
            f"field nor in _LEGACY_SETTING_ALIASES, so a ConfigSetting row stored under the old "
            f"key is silently ignored. For each key, either add "
            f"'<old_key>': '<current_field>' to _LEGACY_SETTING_ALIASES in "
            f"src/teatree/config/resolution.py, or ship a data migration that renames the "
            f"stored rows and drop the key from _RETIRED_SETTING_KEYS."
        )

    def test_every_alias_key_is_recorded_as_retired(self) -> None:
        # The registry is the canonical record of renames: an alias without a
        # retired-key entry means the guard above cannot see it.
        unrecorded = sorted(set(_LEGACY_SETTING_ALIASES) - set(_RETIRED_SETTING_KEYS))
        assert not unrecorded, (
            f"_LEGACY_SETTING_ALIASES key(s) {unrecorded} are missing from _RETIRED_SETTING_KEYS. "
            f"Add each retired key to _RETIRED_SETTING_KEYS so the reachability guard covers it."
        )

    def test_every_alias_target_actually_resolves(self) -> None:
        # A dangling alias (target absent from the DB-home parser registry) would
        # silently drop the row exactly like a missing alias.
        dangling = sorted(
            f"{old} -> {new}" for old, new in _LEGACY_SETTING_ALIASES.items() if new not in OVERLAY_OVERRIDABLE_SETTINGS
        )
        assert not dangling, (
            f"_LEGACY_SETTING_ALIASES target(s) {dangling} are not in OVERLAY_OVERRIDABLE_SETTINGS, "
            f"so a row under the old key resolves to nothing. Point the alias at a DB-home field."
        )


def _live_db_home_keys() -> set[str]:
    return set(OVERLAY_OVERRIDABLE_SETTINGS)


def _unpinned_new_keys(golden: frozenset[str]) -> set[str]:
    return _live_db_home_keys() - golden


def _unaccounted_dropped_keys(golden: frozenset[str]) -> set[str]:
    return set(golden) - _live_db_home_keys() - _RETIRED_SETTING_KEYS - _REMOVED_DEAD_SETTING_KEYS


class TestGoldenSnapshotCatchesSilentDropOrRename:
    """The golden pin turns the opt-in retired-key discipline mechanical (U10/3d#2).

    ``_GOLDEN_SETTING_FIELDS`` is the append-only record of every DB-home settings
    key. A field renamed/removed without recording it (in ``_RETIRED_SETTING_KEYS``
    or ``_REMOVED_DEAD_SETTING_KEYS``) leaves a golden key with no live counterpart —
    caught here — so a stored ``ConfigSetting`` row can never be silently orphaned.
    """

    def test_every_live_db_home_key_is_pinned(self) -> None:
        unpinned = sorted(_unpinned_new_keys(_GOLDEN_SETTING_FIELDS))
        assert not unpinned, (
            f"DB-home setting(s) {unpinned} are live but absent from _GOLDEN_SETTING_FIELDS. "
            f"Add each new field to the golden snapshot in src/teatree/config/resolution.py."
        )

    def test_every_dropped_golden_key_is_recorded_as_retired_or_removed_dead(self) -> None:
        unaccounted = sorted(_unaccounted_dropped_keys(_GOLDEN_SETTING_FIELDS))
        assert not unaccounted, (
            f"Golden DB-home key(s) {unaccounted} are no longer live and are recorded in neither "
            f"_RETIRED_SETTING_KEYS (renamed -> add the alias too) nor _REMOVED_DEAD_SETTING_KEYS "
            f"(removed-dead). A stored ConfigSetting row under each is now silently ignored."
        )

    def test_removed_dead_keys_are_not_live(self) -> None:
        # A removed-dead key that reappeared as a live field is a contradiction:
        # either the field is back (drop it from the bucket) or the bucket is stale.
        resurrected = sorted(_REMOVED_DEAD_SETTING_KEYS & _live_db_home_keys())
        assert not resurrected, f"_REMOVED_DEAD_SETTING_KEYS entries that are live DB-home fields again: {resurrected}"

    def test_retired_and_removed_dead_buckets_are_disjoint(self) -> None:
        both = sorted(_RETIRED_SETTING_KEYS & _REMOVED_DEAD_SETTING_KEYS)
        assert not both, f"Key(s) recorded as BOTH retired (renamed) and removed-dead: {both}"


class TestGoldenSnapshotGuardFiresRed:
    """Anti-vacuity — the golden guard actually catches a synthetic drop and a synthetic add."""

    def test_a_synthetic_unrecorded_drop_is_flagged(self) -> None:
        # A golden key that is neither live nor recorded is exactly the silent-drop
        # class the pin exists to catch.
        golden = _GOLDEN_SETTING_FIELDS | {"synthetic_renamed_away_no_alias"}
        assert "synthetic_renamed_away_no_alias" in _unaccounted_dropped_keys(golden)

    def test_a_recorded_drop_is_not_flagged(self) -> None:
        # Positive control: a dropped key recorded in a bucket is accounted for.
        assert "speed" not in _live_db_home_keys()
        assert "speed" in _RETIRED_SETTING_KEYS
        golden = _GOLDEN_SETTING_FIELDS | {"speed"}
        assert "speed" not in _unaccounted_dropped_keys(golden)

    def test_a_synthetic_unpinned_new_field_is_flagged(self) -> None:
        # A live key missing from the golden pin is caught so a new field can never
        # be added without acknowledging it in the snapshot.
        stripped = _GOLDEN_SETTING_FIELDS - {"mode"}
        assert "mode" in _unpinned_new_keys(stripped)

    def test_the_live_snapshot_is_currently_pinned_and_clean(self) -> None:
        # The shipped state passes both directions — no unpinned add, no unaccounted drop.
        assert _unpinned_new_keys(_GOLDEN_SETTING_FIELDS) == set()
        assert _unaccounted_dropped_keys(_GOLDEN_SETTING_FIELDS) == set()
