"""The per-overlay / env override registries for ``UserSettings``.

The three registries that say WHERE a setting's value may come from and HOW a
stored value is coerced — the DB-home parser registry
(``OVERLAY_OVERRIDABLE_SETTINGS``), the ``T3_*`` env overrides
(``ENV_SETTING_OVERRIDES``), and the safety-posture refusal set
(``SAFETY_POSTURE_KEYS``). Split from the sibling ``settings`` module, which
declares the dataclasses themselves; re-exported from ``teatree.config`` so every
``teatree.config.<name>`` path stays valid.
"""

import os
from collections.abc import Callable
from typing import Any, Final

from teatree.config.agent_enums import AgentHarnessProvider, parse_harness_name
from teatree.config.enums import Autonomy, CriticGateMode, MissingIssuePolicy, Mode, PrReviewBackend, SendProxyMode, Wip
from teatree.config.mr_reminder import parse_mr_reminder_setting
from teatree.config.setting_parsers import (
    _parse_env_bool,
    _parse_env_bool_default_on,
    _parse_env_str_list,
    _parse_handover_mirror_path,
    _parse_harness_skill_exclusions,
    _parse_header_map,
    _parse_overridable_positive_int,
    _parse_str_list,
    _parse_strict_bool,
    _parse_strict_float,
    _parse_strict_int,
    _parse_strict_str,
    _parse_user_identity_aliases,
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
    "write_wip": _parse_strict_int,
    "merge_wip": _parse_strict_int,
    "agent_harness": parse_harness_name,
    "agent_harness_provider": AgentHarnessProvider.parse,
    "harness_skill_exclusions": _parse_harness_skill_exclusions,
    "enforce_regulated_path": _parse_strict_bool,
    "regulated_path_model_allowlist": _parse_str_list,
    "pydantic_ai_request_limit": _parse_strict_int,
    "pydantic_ai_max_tokens": _parse_strict_int,
    # #882 / #885 (F9.5): the headless watchdog + per-ticket budget ceilings, folded
    # off the former Django-settings ``TEATREE_LOOP_WATCHDOG`` / ``TEATREE_TICKET_BUDGET``
    # dicts into the DB-home config tier so ``config_setting get`` reads them.
    "watchdog_max_runtime_seconds": _parse_strict_int,
    "watchdog_max_turns": _parse_strict_int,
    "watchdog_max_cost_usd": _parse_strict_float,
    "ticket_budget_max_cost_usd": _parse_strict_float,
    "subagent_spawn_ceiling": _parse_strict_int,
    "envelope_stop_gate_refusals": _parse_strict_int,
    "agent_max_turns": _parse_strict_int,
    "openai_compatible_base_url": _parse_strict_str,
    "openai_compatible_model": _parse_strict_str,
    "openai_compatible_credential_entry": _parse_strict_str,
    "openai_compatible_lane": _parse_strict_str,
    "openai_compatible_extra_headers": _parse_header_map,
    "openai_compatible_sends_prompt_cache_key": _parse_strict_bool,
    "contribute": _parse_strict_bool,
    "excluded_skills": _parse_str_list,
    "loop_cadence_seconds": _parse_strict_int,
    "worker_quiescing": _parse_strict_bool,
    "require_human_approval_to_merge": _parse_strict_bool,
    "substrate_self_signoff": _parse_strict_bool,
    "substrate_auto_merge_authorized_by": _parse_strict_str,
    "max_open_prs_per_repo_per_ticket": _parse_strict_int,
    "require_human_approval_to_answer": _parse_strict_bool,
    "missing_issue_ref_policy": MissingIssuePolicy.parse,
    "on_behalf_auto_actions": _parse_str_list,
    "review_request_post_disabled": _parse_strict_bool,
    "notify_user_via_bot": _parse_strict_bool,
    "notify_on_post_on_behalf": _parse_strict_bool,
    "user_identity_aliases": _parse_user_identity_aliases,
    "architectural_review_skill": _parse_strict_str,
    "architectural_review_cadence_hours": _parse_strict_int,
    "architectural_review_after_merge_count": _parse_strict_int,
    "review_skill": _parse_strict_str,
    "review_skill_alternates": _parse_str_list,
    "review_backend_cooldown_hours": _parse_strict_int,
    "pr_review_backend": PrReviewBackend.parse,
    "admit_colleague_prs_to_board": _parse_strict_bool,
    "require_review_context": _parse_strict_bool,
    "e2e_mandatory_gate_enabled": _parse_strict_bool,
    "attachment_gate_enabled": _parse_strict_bool,
    "snapshot_baseline_gate_enabled": _parse_strict_bool,
    "gate_relaxation_gate_enabled": _parse_strict_bool,
    "incremental_push_gate": _parse_strict_bool,
    "chrome_devtools_mcp_enabled": _parse_strict_bool,
    "chrome_devtools_headless": _parse_strict_bool,
    "colleague_repo_url_pattern": _parse_strict_str,
    "solo_repo_url_pattern": _parse_strict_str,
    "require_anti_vacuity_attestation": _parse_strict_bool,
    "require_reviewed_state_for_review_request": _parse_strict_bool,
    "require_integration_review": _parse_strict_bool,
    "require_merge_evidence": _parse_strict_bool,
    "require_executed_repro": _parse_strict_bool,
    "require_debt_delta": _parse_strict_bool,
    "require_merge_quality_verdict": _parse_strict_bool,
    "expected_required_contexts": _parse_str_list,
    "critic_gate_mode": CriticGateMode.parse,
    "send_proxy_mode": SendProxyMode.parse,
    "send_proxy_allowlist": _parse_str_list,
    "bulk_close_threshold": _parse_strict_int,
    "e2e_confidence_threshold": _parse_strict_int,
    "scanning_news_skill": _parse_strict_str,
    "scanner_overlay_scope": _parse_str_list,
    "scanning_news_cadence_hours": _parse_strict_int,
    "ask_before_creating_news_tickets": _parse_strict_bool,
    "eval_local_skill": _parse_strict_str,
    "backlog_sweep_skill": _parse_strict_str,
    "ask_before_backlog_sweep_closes": _parse_strict_bool,
    "dogfood_smoke_skill": _parse_strict_str,
    "dogfood_smoke_overlay": _parse_strict_str,
    "schema_readiness_gate_enabled": _parse_strict_bool,
    "self_update_disabled": _parse_strict_bool,
    "auto_update_require_green_main": _parse_strict_bool,
    "auto_disposition_enabled": _parse_strict_bool,
    "auto_update_reinstall": _parse_strict_bool,
    "gitlab_approval_scanner_enabled": _parse_strict_bool,
    "mr_conflict_scan_enabled": _parse_strict_bool,
    "mr_triage_enabled": _parse_strict_bool,
    "review_nag_enabled": _parse_strict_bool,
    "review_resume_reply_enabled": _parse_strict_bool,
    "disk_warn_free_gb": _parse_strict_float,
    "disk_crit_free_gb": _parse_strict_float,
    "ram_warn_avail_gb": _parse_strict_float,
    "ram_crit_avail_gb": _parse_strict_float,
    "adaptive_intake_concurrency_enabled": _parse_strict_bool,
    "intake_ram_reserve_gb": _parse_strict_float,
    "intake_ram_per_agent_gb": _parse_strict_float,
    "disk_cache_allowlist": _parse_str_list,
    "allow_destructive_disk": _parse_strict_bool,
    "artifact_idle_days": _parse_strict_float,
    "worktree_stale_days": _parse_strict_int,
    "allow_destructive_ram": _parse_strict_bool,
    "ram_kill_allowlist": _parse_str_list,
    "task_sweep_disabled": _parse_strict_bool,
    "task_sweep_recheck_interval_hours": _parse_strict_int,
    "target_branch": _parse_strict_str,
    "max_concurrent_local_stacks": _parse_strict_int,
    "worktree_occupancy_gate_enabled": _parse_strict_bool,
    "task_attempt_retention_days": _parse_strict_int,
    "ticket_transition_prune_disabled": _parse_strict_bool,
    "task_result_retention_days": _parse_strict_int,
    "scratch_retention_days": _parse_strict_int,
    "scratch_sweep_root": _parse_strict_str,
    "session_stale_after_hours": _parse_strict_int,
    "provision_step_timeout_seconds": _parse_strict_int,
    "idle_stack_idle_minutes": _parse_strict_int,
    "idle_stack_e2e_recent_minutes": _parse_strict_int,
    "stale_stack_min_age_minutes": _parse_strict_int,
    "clean_ignore": _parse_str_list,
    "notion_write_allowed_roots": _parse_str_list,
    "notion_write_denied_roots": _parse_str_list,
    "slack_voice_classifier_mode": SlackVoiceClassifierMode.parse,
    "pull_main_clone_disabled": _parse_strict_bool,
    "pull_main_clone_cadence_hours": _parse_strict_int,
    "review_nag_max_interval_days": _parse_strict_int,
    "review_exempt_repos": _parse_str_list,
    "review_exempt_repos_count_toward_group_readiness": _parse_strict_bool,
    "require_work_group_batch": _parse_strict_bool,
    "mr_title_regex": _parse_strict_str,
    "issue_implementer_label": _parse_strict_str,
    "issue_implementer_max_concurrent": _parse_strict_int,
    "issue_intake_pass_budget_seconds": _parse_strict_float,
    "trusted_issue_authors": _parse_str_list,
    "independent_reviewer_identities": _parse_str_list,
    "umbrella_issue_labels": _parse_str_list,
    "fleet_claim_enabled": _parse_strict_bool,
    # #3201 PR-3b — the CI-eval self-heal autonomous-fixer OFF switch (DARK flag).
    "ci_eval_heal_autofix_enabled": _parse_strict_bool,
    "outer_loop_enabled": _parse_strict_bool,
    "directive_loop_enabled": _parse_strict_bool,
    # North-star PR-7 — the directive VERIFYING horizon (days) after activation.
    "directive_verify_days": _parse_strict_int,
    "directive_intake_per_tick": _parse_strict_int,
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
    # Directive #2 DB-backup scanner knobs. Cadence / retention use the fail-SAFE
    # coercer (a non-positive or mistyped value degrades to the default), so the
    # "keep a week of backups" bound cannot be configured away to 0.
    "dashboard_instance_label": _parse_strict_str,
    "dashboard_logo": _parse_strict_str,
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
    "contribute_plugin_dir": _parse_strict_bool,
    "dream_automation_asks": _parse_strict_bool,
    "dream_compliance_escalate": _parse_strict_bool,
    "dream_compliance_measure": _parse_strict_bool,
    "dream_cross_link": _parse_strict_bool,
    "dream_decay": _parse_strict_bool,
    "dream_derive_evals": _parse_strict_bool,
    "dream_memory_promote": _parse_strict_bool,
    "dream_merge": _parse_strict_bool,
    "dream_propose_evals": _parse_strict_bool,
    "dream_reindex": _parse_strict_bool,
    "dream_validate_live": _parse_strict_bool,
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
    # DB-home cutover: ``orchestrator_bash_gate_enabled``'s reader
    # (``teatree_gate._gate_key_is_enabled``) is already DB-first via ``cold_reader``
    # (toml fallback for the cold self-rescue). Its former carve-out siblings
    # ``privacy`` / ``timezone`` were retired reader-less (#4203).
    "orchestrator_bash_gate_enabled": _parse_strict_bool,
    "orphan_group_min_age_hours": _parse_strict_int,
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
    "single_branch_repos": _parse_str_list,
    # Opt-in (#3502) render-in-an-engaged-session flag. Bash-read DB-only by the
    # statusline hook (sqlite3 CLI); strict bool, default OFF (#256 unchanged when unset).
    "statusline_engaged_render": _parse_strict_bool,
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
    # #3644 Default-ON adaptive admission governor; false is the kill-switch that
    # reverts admission to the pre-governor static behaviour. Per-overlay overridable.
    "admission_governor_enabled": _parse_strict_bool,
    # #4508 Pressure at which the EXPENSIVE agent class is shed while the cheap drain
    # keeps running; 1.0 collapses SHED into HALT (the rollback lever). Per-overlay overridable.
    "admission_pressure_shed_at": _parse_strict_float,
    # #4816 Whether the TOKEN brakes apply at all; false leaves load + memory only, so
    # standing down an irrelevant quota signal never disarms the box. Per-overlay overridable.
    "admission_quota_brake_enabled": _parse_strict_bool,
    # #4816 The metered lane's spend ceiling in TOKENS over the window below; 0 =
    # UNSET, so the metered brake is inert until an operator sets it. Per-overlay overridable.
    "metered_token_ceiling": _parse_strict_int,
    # #4816 The window the metered token ceiling is measured over. Per-overlay overridable.
    "metered_spend_window_hours": _parse_strict_int,
    # #4098 Bound on the CHEAP-phase admission lane — how many read-only/work-retiring
    # phase agents stay admissible while the governor brakes the expensive class. 0
    # disables the exemption (cheap is braked like expensive). Per-overlay overridable.
    "cheap_phase_admission_ceiling": _parse_strict_int,
    # #4374 Slots in the governor's ceiling only the DRAINING class may occupy, so
    # expensive work cannot fill the whole factory and leave zero reviews running.
    # Clamped to at most ceiling-2, so the expensive class always keeps two slots — a
    # 4-core box's ceiling is 2, where ceiling-1 would leave it a single one (#4407).
    # 0 restores first-come allocation. Per-overlay overridable.
    "drain_slot_reservation": _parse_strict_int,
    # #4163 RAM one pytest-xdist worker is sized at when the governor derives the
    # per-agent worker cap — the measured p90 worker RSS. A non-positive value drops
    # the memory term and leaves the cores-derived bound. Per-overlay overridable.
    "test_worker_ram_gb": _parse_strict_float,
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
#: Keys whose reader takes NO overlay, so a per-overlay row is an opinion the box cannot
#: honour — a loop timer is one clock per box. Hand-maintained beside the other registries
#: for the same cold-path reason (a module-scope ``derive_*()`` would drag pydantic onto
#: every cold-hook read); ``schema.derive_box_global_settings`` keeps this copy honest.
BOX_GLOBAL_SETTINGS: frozenset[str] = frozenset(
    {
        "harness_skill_exclusions",
        "loop_cadence_seconds",
        "scanning_news_cadence_hours",
        "snapshot_warmer_max_age_days",
    }
)


ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
    "T3_WIP": ("wip", Wip.parse),
    "T3_WRITE_WIP": ("write_wip", int),
    "T3_MERGE_WIP": ("merge_wip", int),
    "T3_AGENT_HARNESS": ("agent_harness", parse_harness_name),
    "T3_AGENT_HARNESS_PROVIDER": ("agent_harness_provider", AgentHarnessProvider.parse),
    "T3_ENFORCE_REGULATED_PATH": ("enforce_regulated_path", _parse_env_bool),
    "T3_OPENAI_COMPATIBLE_BASE_URL": ("openai_compatible_base_url", str),
    "T3_OPENAI_COMPATIBLE_MODEL": ("openai_compatible_model", str),
    "T3_OPENAI_COMPATIBLE_LANE": ("openai_compatible_lane", str),
    "T3_MISSING_ISSUE_POLICY": ("missing_issue_ref_policy", MissingIssuePolicy.parse),
    "T3_ON_BEHALF_AUTO_ACTIONS": ("on_behalf_auto_actions", _parse_env_str_list),
    "T3_REVIEW_SKILL": ("review_skill", str),
    # #3895 shipped these two master gates ON, so each needs the same one-command
    # kill switch its sibling loop gates already had — an operator stopping a
    # default-ON loop cannot be made to write a DB row first.
    "T3_DIRECTIVE_LOOP_ENABLED": ("directive_loop_enabled", _parse_env_bool),
    "T3_TRUSTED_ISSUE_AUTHORS": ("trusted_issue_authors", _parse_env_str_list),
    "T3_FLEET_CLAIM_ENABLED": ("fleet_claim_enabled", _parse_env_bool),
    "T3_DREAM_AUTOMATION_ASKS": ("dream_automation_asks", _parse_env_bool),
    "T3_DREAM_COMPLIANCE_ESCALATE": ("dream_compliance_escalate", _parse_env_bool),
    "T3_DREAM_COMPLIANCE_MEASURE": ("dream_compliance_measure", _parse_env_bool),
    "T3_DREAM_CROSS_LINK": ("dream_cross_link", _parse_env_bool),
    "T3_DREAM_DECAY": ("dream_decay", _parse_env_bool),
    "T3_DREAM_DERIVE_EVALS": ("dream_derive_evals", _parse_env_bool),
    "T3_DREAM_MEMORY_PROMOTE": ("dream_memory_promote", _parse_env_bool),
    "T3_DREAM_MERGE": ("dream_merge", _parse_env_bool),
    "T3_DREAM_PROPOSE_EVALS": ("dream_propose_evals", _parse_env_bool),
    "T3_DREAM_REINDEX": ("dream_reindex", _parse_env_bool),
    "T3_DREAM_VALIDATE_LIVE": ("dream_validate_live", _parse_env_bool),
    "T3_LOOP_AUTO_UPDATE": ("auto_update_reinstall", _parse_env_bool),
    "T3_ORCHESTRATE_CLAIM_ENABLED": ("orchestrate_claim_enabled", _parse_env_bool),
    "T3_FACTORY_SCORE_ENABLED": ("factory_score_enabled", _parse_env_bool),
    "T3_OUTER_LOOP_ENABLED": ("outer_loop_enabled", _parse_env_bool),
    "T3_BOOST_CONCURRENCY": ("boost_concurrency", _parse_strict_int),
    "T3_WORKER_QUIESCING": ("worker_quiescing", _parse_env_bool),
    "T3_CONTRIBUTE": ("contribute_plugin_dir", _parse_env_bool),
    "T3_HOOK_FETCH_TITLES": ("hook_fetch_titles", _parse_env_bool_default_on),
    "T3_AUTOLOAD": ("autoload", _parse_env_bool),
}


# The ``UserSettings`` fields whose WRITE is itself an authorization / delegation /
# fail-closed-boundary act — not a tunable knob. Writing one of these does not merely
# CONFIGURE a gate: it grants authority (``substrate_auto_merge_authorized_by`` — "the
# config write IS the human authorization"), delegates a keystone sign-off
# (``substrate_self_signoff``), disarms an egress/on-behalf pre-gate
# (a permitting posture, ``on_behalf_auto_actions``), or WIDENS a
# fail-closed intake / egress / regulated / maker≠checker allowlist
# (``trusted_issue_authors``, ``send_proxy_allowlist``, ``regulated_path_model_allowlist``,
# ``independent_reviewer_identities``), raises the global
# autonomy posture (``autonomy``, ``enforce_regulated_path``), or relaxes an
# autonomous-close boundary (``bulk_close_threshold``). The MCP ``config_setting_set``
# surface REFUSES every key here by declared EFFECT (``teatree.mcp.write_tools`` reads
# this set), so a shell-denied MCP agent can never self-grant merge delegation or widen
# the fail-closed intake allowlist by classifying keys via a name-glob that misses them
# (F9.1). This is EFFECT-based, not name-shaped: the companion conformance test
# (``tests/teatree_mcp/test_write_tools_refusals.py``) walks every ``UserSettings`` field
# and fails CLOSED if a delegation/allowlist/authorization-shaped field is in neither this
# set nor the explicit reviewed ``teatree.mcp.write_tools.MCP_SETTABLE_OK`` allowlist — so
# a future safety-posture field can never ship silently MCP-settable.
#: Which ``T3_*`` var carries each env-overridable setting — ``ENV_SETTING_OVERRIDES`` read
#: the other way round, for the surfaces that start from a setting name.
ENV_VAR_BY_SETTING: Final[dict[str, str]] = {
    field_name: env_var for env_var, (field_name, _parser) in ENV_SETTING_OVERRIDES.items()
}


def env_pinned_value(env_var: str) -> str | None:
    """What *env_var* pins in THIS process, or ``None`` when it pins nothing.

    An exported-but-EMPTY var pins only what its parser can represent. ``T3_FOO=`` is how a
    shell neutralises an inherited pin, and reading it as a bool resolved every
    ``T3_DREAM_*`` phase to ``False`` and stopped the memory phases running — but for a list
    the empty string IS the value, the empty allowlist. The parser answers which, so the two
    cases cannot drift apart the way a second registry of "empty means unset" keys would.

    Only the EMPTY case consults the parser: a non-empty value is handed on unexamined, so a
    typo (``T3_ENFORCE_REGULATED_PATH=treu``) still raises where the operator can see it
    rather than falling silently through to the tier below.

    The resolver and :func:`env_pin` share this one predicate so a surface can never report
    a pin the resolver does not apply.
    """
    raw = os.environ.get(env_var)
    if raw is None or raw.strip():
        return raw
    _field, parser = ENV_SETTING_OVERRIDES.get(env_var, ("", None))
    if parser is None:
        return None
    try:
        parser(raw)
    except ValueError:
        return None
    return raw


#: Settings a code path deliberately reads from the SHIPPED default rather than the resolver,
#: mapped to the sentence a grid shows instead of an edit box. A stored row for one of these
#: is written and then read by nobody, so an editable control in front of it invites an
#: operator to believe they armed something. Declared here because it is what the dash needs
#: to render, not a fact any walk can derive from the pinning call site.
CODE_PINNED_SETTINGS: Final[dict[str, str]] = {
    "allow_destructive_disk": "pinned to its shipped value in the resource-pressure scanner",
    "allow_destructive_ram": "pinned to its shipped value in the resource-pressure scanner",
}


#: Settings whose only consumer is prose — a skill or a document reads the value, so no
#: `src` reader exists and none is missing. Mapped to the reason, because "nothing reads
#: this" and "a human reads this" are the same measurement and opposite verdicts.
PROSE_CONSUMED_SETTINGS: Final[dict[str, str]] = {
    "e2e_confidence_threshold": "read by the `/t3:e2e` verify-review loop, which is agent prose rather than a gate",
}

#: Every key DECLARED to have no `src` reader, whichever reason it carries. The readership
#: lanes subtract this; `teatree.quality` cannot, being a foundation layer that may not
#: reach config — which is why the matcher there stays a pure derivation and the policy
#: lives here, named once for both lanes.
READER_LESS_SETTINGS: Final[frozenset[str]] = frozenset(CODE_PINNED_SETTINGS) | frozenset(PROSE_CONSUMED_SETTINGS)


def code_pin_refusal(key: str) -> str:
    """Why *key* cannot usefully be written from a grid, or ``""`` when it can."""
    return CODE_PINNED_SETTINGS.get(key, "")


def env_pin(key: str) -> str:
    """The ``T3_*`` var pinning *key* in THIS process, or ``""`` when none is.

    The env tier outranks every stored tier, so while such a var carries a value a DB write
    to *key* lands in a layer nothing reads back: the write reports success and changes
    nothing an operator can observe. Every surface that offers or accepts an edit asks here.
    """
    env_var = ENV_VAR_BY_SETTING.get(key, "")
    return env_var if env_var and env_pinned_value(env_var) is not None else ""


SAFETY_POSTURE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "autonomy",
        "enforce_regulated_path",
        "regulated_path_model_allowlist",
        "substrate_self_signoff",
        "substrate_auto_merge_authorized_by",
        "on_behalf_auto_actions",
        "send_proxy_allowlist",
        "trusted_issue_authors",
        "independent_reviewer_identities",
        "bulk_close_threshold",
    }
)
