"""TeaTree config dataclasses + the per-overlay / env override registries.

``UserSettings`` (the ``[teatree]`` table), ``TeaTreeConfig``, ``OverlayEntry``,
the field ``_parse_*`` coercers, and the two override registries
(``OVERLAY_OVERRIDABLE_SETTINGS`` / ``ENV_SETTING_OVERRIDES``). Split out of the
package module for the module-health LOC cap; re-exported from
``teatree.config`` so every ``teatree.config.<name>`` path stays valid.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from teatree.config.agent_enums import AgentHarnessProvider, AgentRuntime, EvalCredential, parse_harness_name
from teatree.config.enums import (
    Autonomy,
    CriticGateMode,
    MissingIssuePolicy,
    Mode,
    OnBehalfPostMode,
    SendProxyMode,
    TeamsDisplay,
    Wip,
)
from teatree.config.mr_reminder import parse_mr_reminder_setting
from teatree.config.setting_parsers import (
    _parse_env_bool,
    _parse_env_bool_default_on,
    _parse_env_positive_int,
    _parse_env_str_list,
    _parse_env_teams_display,
    _parse_handover_mirror_path,
    _parse_overridable_positive_int,
    _parse_str_list,
    _parse_strict_bool,
    _parse_strict_float,
    _parse_strict_int,
    _parse_strict_str,
    _parse_user_identity_aliases,
)
from teatree.config.settings_groups import (
    _IdentityRoutingSettings,
    _LoopAndTeamsSettings,
    _LoopFlagAndCredentialSettings,
    _ModeHarnessSettings,
    _OnBehalfSettings,
    _PrePublishGateSettings,
    _ProvisioningSettings,
    _QualityGateSettings,
    _ResourcePressureSettings,
    _ScannerSettings,
    _WorkspaceCoreSettings,
)
from teatree.config.speak import parse_speak_setting
from teatree.types import SlackVoiceClassifierMode

# The DB-home parser registry (#1775 hard partition). Every DB-home
# ``UserSettings`` field (see ``config/homes.py``) has an entry here: the parser
# coerces a stored ``ConfigSetting`` JSON value to the field's type. This registry
# is the SOLE source for a DB-home field — its ``[teatree]`` / ``[overlays.<name>]``
# TOML tables are NOT read on resolution; a DB-home key left in TOML is ignored on
# read (migrate it with ``config_setting import``). ``_db_setting_overrides`` consults this to
# decide which ``ConfigSetting`` rows supply a value and reuses each entry's
# parser; a row for a key absent here is ignored. Per DB-home field the chain is
# ``env -> ConfigSetting (overlay then global) -> dataclass default``. A
# fitness test asserts this registry covers exactly the DB-home set (no TOML-home
# key, every DB-home key present).
OVERLAY_OVERRIDABLE_SETTINGS: dict[str, Callable[[Any], Any]] = {
    # Stored as a path STRING (JSONField holds no Path); config.worktree_root() is
    # the typed accessor that expanduser()-wraps it and applies the per-overlay default.
    "workspace_dir": _parse_strict_str,
    "mode": Mode.parse,
    "autonomy": Autonomy.parse,
    "wip": Wip.parse,
    "agent_runtime": AgentRuntime.parse,
    "agent_harness": parse_harness_name,
    "agent_harness_provider": AgentHarnessProvider.parse,
    "enforce_regulated_path": _parse_strict_bool,
    "regulated_path_model_allowlist": _parse_str_list,
    "pydantic_ai_request_limit": _parse_strict_int,
    "orca_router_pass_path": _parse_strict_str,
    "orca_router_lane": _parse_strict_str,
    "orca_router_name": _parse_strict_str,
    "eval_credential": EvalCredential.parse,
    "contribute": _parse_strict_bool,
    "excluded_skills": _parse_str_list,
    "loop_cadence_seconds": _parse_strict_int,
    "loop_runner_enabled": _parse_strict_bool,
    "teams_enabled": _parse_strict_bool,
    "teams_max_panes": _parse_overridable_positive_int(1),
    "teams_idle_minutes": _parse_overridable_positive_int(30),
    "teams_display": TeamsDisplay.parse,
    "require_human_approval_to_merge": _parse_strict_bool,
    "substrate_self_signoff": _parse_strict_bool,
    "max_open_prs_per_repo_per_ticket": _parse_strict_int,
    "require_human_approval_to_answer": _parse_strict_bool,
    "on_behalf_post_mode": OnBehalfPostMode.parse,
    "missing_issue_ref_policy": MissingIssuePolicy.parse,
    "on_behalf_auto_actions": _parse_str_list,
    "review_request_post_disabled": _parse_strict_bool,
    "notify_user_via_bot": _parse_strict_bool,
    "notify_on_post_on_behalf": _parse_strict_bool,
    "user_identity_aliases": _parse_user_identity_aliases,
    "architectural_review_disabled": _parse_strict_bool,
    "architectural_review_skill": _parse_strict_str,
    "architectural_review_cadence_hours": _parse_strict_int,
    "architectural_review_after_merge_count": _parse_strict_int,
    "review_skill": _parse_strict_str,
    "require_review_context": _parse_strict_bool,
    "e2e_mandatory_gate_enabled": _parse_strict_bool,
    "attachment_gate_enabled": _parse_strict_bool,
    "snapshot_baseline_gate_enabled": _parse_strict_bool,
    "gate_relaxation_gate_enabled": _parse_strict_bool,
    "incremental_push_gate": _parse_strict_bool,
    "chrome_devtools_mcp_enabled": _parse_strict_bool,
    "colleague_repo_url_pattern": _parse_strict_str,
    "solo_repo_url_pattern": _parse_strict_str,
    "require_anti_vacuity_attestation": _parse_strict_bool,
    "require_reviewed_state_for_review_request": _parse_strict_bool,
    "require_integration_review": _parse_strict_bool,
    "require_merge_evidence": _parse_strict_bool,
    "require_plan_adequacy": _parse_strict_bool,
    "require_executed_repro": _parse_strict_bool,
    "require_debt_delta": _parse_strict_bool,
    "require_merge_quality_verdict": _parse_strict_bool,
    "expected_required_contexts": _parse_str_list,
    "critic_gate_mode": CriticGateMode.parse,
    "send_proxy_mode": SendProxyMode.parse,
    "send_proxy_allowlist": _parse_str_list,
    "bulk_close_threshold": _parse_strict_int,
    "require_rubric_verification": _parse_strict_bool,
    "require_spec_coverage": _parse_strict_bool,
    "e2e_confidence_threshold": _parse_strict_int,
    "scanning_news_disabled": _parse_strict_bool,
    "scanning_news_skill": _parse_strict_str,
    "scanning_news_cadence_hours": _parse_strict_int,
    "ask_before_creating_news_tickets": _parse_strict_bool,
    "eval_local_disabled": _parse_strict_bool,
    "eval_local_skill": _parse_strict_str,
    "eval_local_cadence_hours": _parse_strict_int,
    "backlog_sweep_disabled": _parse_strict_bool,
    "backlog_sweep_skill": _parse_strict_str,
    "backlog_sweep_cadence_hours": _parse_strict_int,
    "ask_before_backlog_sweep_closes": _parse_strict_bool,
    "dogfood_smoke_disabled": _parse_strict_bool,
    "dogfood_smoke_skill": _parse_strict_str,
    "dogfood_smoke_cadence_hours": _parse_strict_int,
    "dogfood_smoke_overlay": _parse_strict_str,
    "self_update_disabled": _parse_strict_bool,
    "self_update_cadence_hours": _parse_strict_int,
    "auto_update_reinstall": _parse_strict_bool,
    "auto_update_require_green_main": _parse_strict_bool,
    "resource_pressure_disabled": _parse_strict_bool,
    "resource_pressure_cadence_minutes": _parse_strict_int,
    "resource_pressure_min_free_interval_minutes": _parse_strict_int,
    "disk_warn_free_gb": _parse_strict_float,
    "disk_crit_free_gb": _parse_strict_float,
    "ram_warn_avail_gb": _parse_strict_float,
    "ram_crit_avail_gb": _parse_strict_float,
    "disk_cache_allowlist": _parse_str_list,
    "allow_destructive_disk": _parse_strict_bool,
    "worktree_stale_days": _parse_strict_int,
    "max_worktree_gc_per_tick": _parse_strict_int,
    "allow_destructive_ram": _parse_strict_bool,
    "ram_kill_allowlist": _parse_str_list,
    "task_sweep_disabled": _parse_strict_bool,
    "task_sweep_recheck_interval_hours": _parse_strict_int,
    "max_concurrent_local_stacks": _parse_strict_int,
    "provision_step_timeout_seconds": _parse_strict_int,
    "idle_stack_reaper_disabled": _parse_strict_bool,
    "idle_stack_idle_minutes": _parse_strict_int,
    "idle_stack_reaper_cadence_minutes": _parse_strict_int,
    "idle_stack_e2e_recent_minutes": _parse_strict_int,
    "stale_stack_min_age_minutes": _parse_strict_int,
    "local_stack_queue_disabled": _parse_strict_bool,
    "local_stack_queue_max_attempts": _parse_strict_int,
    "clean_ignore": _parse_str_list,
    "slack_voice_classifier_mode": SlackVoiceClassifierMode.parse,
    "pull_main_clone_disabled": _parse_strict_bool,
    "pull_main_clone_cadence_hours": _parse_strict_int,
    "review_nag_enabled": _parse_strict_bool,
    "review_request_dedup_window_days": _parse_overridable_positive_int(30),
    "review_request_dedup_max_pages": _parse_overridable_positive_int(5),
    "mr_title_regex": _parse_strict_str,
    "issue_implementer_enabled": _parse_strict_bool,
    "issue_implementer_label": _parse_strict_str,
    "issue_implementer_require_label": _parse_strict_bool,
    "issue_implementer_max_concurrent": _parse_strict_int,
    "issue_implementer_cadence_hours": _parse_strict_int,
    "trusted_issue_authors": _parse_str_list,
    "fleet_claim_enabled": _parse_strict_bool,
    "auto_disposition_enabled": _parse_strict_bool,
    "limit_autorecovery_enabled": _parse_strict_bool,
    "outer_loop_enabled": _parse_strict_bool,
    "directive_loop_enabled": _parse_strict_bool,
    # North-star PR-7 — the directive VERIFYING horizon (days) after activation.
    "directive_verify_days": _parse_strict_int,
    # T4-PR-3 — the autoresearch outer-loop runtime bounds: the post-implement
    # measurement horizon (days), the weekly experiment cap, and the convergence
    # brake (park after N consecutive non-KEPT decisions). All DB-home,
    # per-overlay overridable — an overlay can trial the loop on its own budget.
    "outer_loop_measure_days": _parse_strict_int,
    "outer_loop_max_per_week": _parse_strict_int,
    "outer_loop_stop_after_consecutive_failures": _parse_strict_int,
    # T4-PR-2 — the SIG-PR-2 recipe/score seam OFF switch (DARK feature flag) and
    # the human-approved recipe sha the score stamps against. Both DB-home,
    # per-overlay overridable — an overlay can trial the score while the global stays OFF.
    "factory_score_enabled": _parse_strict_bool,
    "approved_recipe_sha": _parse_strict_str,
    "auto_disposition_max_closes_per_tick": _parse_strict_int,
    # Directive #2 DB-backup scanner knobs. Cadence / retention use the fail-SAFE
    # coercer (a non-positive or mistyped value degrades to the default), so the
    # "keep a week of backups" bound cannot be configured away to 0.
    "db_backup_disabled": _parse_strict_bool,
    "db_backup_cadence_hours": _parse_overridable_positive_int(24),
    "db_backup_retention_days": _parse_overridable_positive_int(7),
    "orchestrate_claim_enabled": _parse_strict_bool,
    "boost_concurrency": _parse_strict_int,
    # #1775 newly-DB-home (formerly file-only): these now resolve from the DB store.
    "agent_signature": _parse_strict_bool,
    "admin_autologin_enabled": _parse_strict_bool,
    "claude_chrome": _parse_strict_bool,
    "repo_mode": _parse_strict_str,
    "ban_close_trailers_on_namespaces": _parse_str_list,
    "billing_cycle_anchor_day": _parse_strict_int,
    "sdk_monthly_credit_usd": _parse_strict_float,
    # #2697 — bypass readers migrated from bespoke ``os.environ`` reads to DB-home.
    "gitlab_approval_scanner_enabled": _parse_strict_bool,
    "contribute_plugin_dir": _parse_strict_bool,
    "dream_propose_evals": _parse_strict_bool,
    "hook_fetch_titles": _parse_strict_bool,
    # Per-account ``pass`` routing for the Anthropic credentials (llm/credentials.py):
    # an ORDERED LIST of ``pass`` entries the routing selector fans out over per
    # overlay (empty list = no override, credential keeps its built-in path).
    "anthropic_oauth_pass_paths": _parse_str_list,
    "anthropic_api_key_pass_paths": _parse_str_list,
    # DB-home cutover: ``check_updates``'s sole reader ``check_for_updates``
    # runs pre-Django but now reads the DB via ``cold_reader`` (Django-free), so a
    # stored ``check_updates=false`` IS honoured. DB-home, seeded by ``t3 setup``.
    "check_updates": _parse_strict_bool,
    # DB-home cutover: ``timezone`` was tagged "needed to open the DB", but Django
    # ``settings.py`` hardcodes ``TIME_ZONE = "UTC"`` and configures ``DATABASES``
    # without reading it — so it is not a bootstrap dep. It has no live reader
    # (DB-home for partition consistency). (The former sibling ``worktrees_dir``
    # was removed — it duplicated ``worktree_root()``'s "where worktrees are
    # created" role with a divergent default; see ``tests/config/
    # test_removed_dead_settings.py``.)
    "timezone": _parse_strict_str,
    # DB-home cutover: the last two per-overlay-TOML-overridable carve-out
    # fields move to DB-home (per-overlay via a ``ConfigSetting`` overlay-scope row).
    # ``orchestrator_bash_gate_enabled``'s reader (``teatree_gate._gate_key_is_enabled``)
    # is already DB-first via ``cold_reader`` (toml fallback for the cold self-rescue);
    # ``privacy`` has no live production reader.
    "orchestrator_bash_gate_enabled": _parse_strict_bool,
    "privacy": _parse_strict_str,
    # DB-home cutover: ``handover_mirror_path``. The pre-Django reader
    # (``hook_router`` SessionStart bootstrap) now reads the canonical sqlite via
    # ``cold_reader`` — which fails open to ``_default_handover_mirror_path()``, the
    # exact path ``write_mirror`` uses when unset — so the "read when the DB is
    # unreachable" carve-out is satisfied without TOML. Stored as a path STRING.
    "handover_mirror_path": _parse_handover_mirror_path,
    # DB-home cutover: ``statusline_chain``. The bash statusline hook now
    # reads it from the canonical sqlite via the ``sqlite3`` CLI + ``json_each``
    # (``_statusline_chain_db``) — no importable teatree python, no TOML parse.
    "statusline_chain": _parse_str_list,
    # DB-home cutover: ``autoload`` (#256 engagement flag). Read DB-only via
    # ``cold_reader`` (Python hook ``teatree_settings.autoload_enabled``) and the
    # ``sqlite3`` CLI (bash ``statusline.sh._autoload_db_value``); a ``[teatree]
    # autoload`` TOML value is ignored on read. Strict bool, default OFF.
    "autoload": _parse_strict_bool,
    # Parallel ticket-workspace provisioning speed + resource-aware admission.
    # Fast steps (symlinks, settings, a compose override) default to this short
    # ceiling instead of the uniform 1800s one; a step opts into the long
    # ceiling via ``ProvisionStep.heavy``. Per-overlay overridable.
    "provision_fast_step_timeout_seconds": _parse_strict_int,
    # nCPU-derived default concurrency cap for parallel worktree provisioning
    # (0 = auto-derive from ``os.cpu_count()`` at each read, never persisted as
    # a magic number that drifts from the actual host). A positive value pins
    # an explicit cap. Per-overlay overridable.
    "provision_max_concurrency": _parse_strict_int,
    # RAM-used-percent ceiling above which a NEW provision is held (queued, not
    # started) rather than admitted — mirrors ``DEFAULT_RAM_USED_CEILING_PCT``
    # in the self-improve budget gate. Per-overlay overridable.
    "provision_ram_ceiling_percent": _parse_strict_int,
    # A provision whose total duration exceeds this many seconds triggers a
    # best-effort out-of-band user alert (the same egress
    # ``provision_timebox.alert_provision_user`` uses) so a regression in
    # provisioning speed is never silently absorbed. Per-overlay overridable.
    "provision_slow_threshold_seconds": _parse_strict_int,
    # Reference-DB DSLR snapshots older than this many days are STALE — the
    # snapshot-warmer loop refreshes them out-of-band; a ticket-critical-path
    # provision facing a stale/missing snapshot fails fast with a pointer to
    # the warmer instead of silently paying the slow restore+migrate path.
    # Per-overlay overridable.
    "snapshot_warmer_max_age_days": _parse_strict_int,
    "snapshot_warmer_disabled": _parse_strict_bool,
    # DB-home cutover: the last two carve-out fields — the nested
    # structured tables ``speak`` / ``mr_reminder``. Each parser validates + stores
    # the CANONICAL ``to_dict()`` JSON object; the resolver rebuilds the dataclass
    # bespoke (``resolution._BESPOKE_STRUCTURED_FIELDS``) since a dict cannot
    # flat-replace the dataclass field. The cold Stop-hook ``speak`` reader uses
    # ``cold_reader.read_setting`` (a dict), so neither needs TOML.
    "speak": parse_speak_setting,
    "mr_reminder": parse_mr_reminder_setting,
}

# TOML-home keys that ALSO support a per-overlay ``[overlays.<name>]`` override.
# DB-home cutover emptied this: the per-overlay override of a setting now
# lives entirely in the DB (an overlay-scoped ``ConfigSetting`` row). ``speak`` was
# never here — its per-overlay override merges bespoke (now off the DB overlay-scope
# row, ``resolution._resolve_speak_db``); every other field is DB-home. Discovery
# still unions this with the DB-home registry; with it empty the union is just the
# DB-home registry.
TOML_OVERLAY_OVERRIDABLE_SETTINGS: dict[str, Callable[[Any], Any]] = {}

# ``T3_*`` env vars that win over both the per-overlay override and the
# global setting. Mapped to ``(UserSettings field, parser)``.
ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
    "T3_WIP": ("wip", Wip.parse),
    "T3_AGENT_RUNTIME": ("agent_runtime", AgentRuntime.parse),
    "T3_AGENT_HARNESS": ("agent_harness", parse_harness_name),
    "T3_AGENT_HARNESS_PROVIDER": ("agent_harness_provider", AgentHarnessProvider.parse),
    "T3_ENFORCE_REGULATED_PATH": ("enforce_regulated_path", _parse_env_bool),
    "T3_ORCA_ROUTER_LANE": ("orca_router_lane", str),
    "T3_ORCA_ROUTER_NAME": ("orca_router_name", str),
    "T3_EVAL_CREDENTIAL": ("eval_credential", EvalCredential.parse),
    "T3_ON_BEHALF_POST_MODE": ("on_behalf_post_mode", OnBehalfPostMode.parse),
    "T3_MISSING_ISSUE_POLICY": ("missing_issue_ref_policy", MissingIssuePolicy.parse),
    "T3_ON_BEHALF_AUTO_ACTIONS": ("on_behalf_auto_actions", _parse_env_str_list),
    "T3_REVIEW_SKILL": ("review_skill", str),
    "T3_ISSUE_IMPLEMENTER_ENABLED": ("issue_implementer_enabled", _parse_env_bool),
    "T3_ISSUE_IMPLEMENTER_REQUIRE_LABEL": ("issue_implementer_require_label", _parse_env_bool),
    "T3_TRUSTED_ISSUE_AUTHORS": ("trusted_issue_authors", _parse_env_str_list),
    "T3_FLEET_CLAIM_ENABLED": ("fleet_claim_enabled", _parse_env_bool),
    "T3_LOOP_AUTO_UPDATE": ("auto_update_reinstall", _parse_env_bool),
    "T3_ORCHESTRATE_CLAIM_ENABLED": ("orchestrate_claim_enabled", _parse_env_bool),
    "T3_FACTORY_SCORE_ENABLED": ("factory_score_enabled", _parse_env_bool),
    "T3_OUTER_LOOP_ENABLED": ("outer_loop_enabled", _parse_env_bool),
    "T3_LIMIT_AUTORECOVERY_ENABLED": ("limit_autorecovery_enabled", _parse_env_bool),
    "T3_BOOST_CONCURRENCY": ("boost_concurrency", _parse_strict_int),
    "T3_LOOP_RUNNER_ENABLED": ("loop_runner_enabled", _parse_env_bool),
    "T3_TEAMS_ENABLED": ("teams_enabled", _parse_env_bool),
    "T3_TEAMS_MAX_PANES": ("teams_max_panes", _parse_env_positive_int(1)),
    "T3_TEAMS_IDLE_MINUTES": ("teams_idle_minutes", _parse_env_positive_int(30)),
    "T3_TEAMS_DISPLAY": ("teams_display", _parse_env_teams_display),
    "T3_CONTRIBUTE": ("contribute_plugin_dir", _parse_env_bool),
    "T3_HOOK_FETCH_TITLES": ("hook_fetch_titles", _parse_env_bool_default_on),
    "T3_AUTOLOAD": ("autoload", _parse_env_bool),
}


@dataclass
class OverlayEntry:
    name: str
    overlay_class: str
    project_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def canonical_overlay_name(name: str) -> str:
        """The route/dedup key for an overlay identifier: ``name`` minus the ``t3-`` prefix.

        A TOML overlay table and the ``t3-``-prefixed entry point that registers
        it address the same overlay; this strip is the key under which CLI
        sub-apps are routed and deduplicated so the pair cannot register two
        sub-apps.

        This is the CLI-routing key only — distinct from the legacy-alias fold
        in :func:`_match_canonical_ep`, which maps a bare ``[overlays.<alias>]``
        table onto an installed entry point. Keep the two separate.
        """
        return name.removeprefix("t3-")


@dataclass
class UserSettings(
    _WorkspaceCoreSettings,
    _ModeHarnessSettings,
    _LoopAndTeamsSettings,
    _OnBehalfSettings,
    _IdentityRoutingSettings,
    _QualityGateSettings,
    _ScannerSettings,
    _ResourcePressureSettings,
    _ProvisioningSettings,
    _PrePublishGateSettings,
    _LoopFlagAndCredentialSettings,
):
    """The ``[teatree]`` settings — the FLAT, 160-field persisted contract.

    The fields are declared across ~11 private in-file group bases above purely for
    readability; ``UserSettings`` is the sole public API and ``dataclasses.fields()``
    stays inheritance-transparent, so the flat field namespace (DB ``ConfigSetting.key``,
    env overrides, cold sqlite3 readers, the rename-guard and golden pin) is unchanged.

    CLAUDE.md's "composition over mixins" targets behaviour-carrying classes; these bases
    are pure data-declaration with no behaviour, and the flat schema IS the persisted
    contract, so grouping them as declaration bases (not composed attributes) is the
    deliberate divergence — nesting would be a ~160-key data migration, not a refactor.
    The ``tests/config/test_settings_group_partition.py`` guard pins the group field sets
    pairwise-disjoint and their union == ``dataclasses.fields(UserSettings)`` so a
    silently-shadowed duplicate field can never slip in.
    """


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)
