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

from teatree.config.agent_enums import (
    AgentHarness,
    AgentHarnessProvider,
    AgentRuntime,
    EvalCredential,
    parse_harness_name,
)
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
from teatree.config.mr_reminder import MrReminderConfig, parse_mr_reminder_setting
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
from teatree.config.settings_loop_flags import _LoopFlagAndCredentialSettings
from teatree.config.speak import parse_speak_setting
from teatree.types import DEFAULT_MR_TITLE_REGEX, SlackVoiceClassifierMode, SpeakConfig

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
    "worker_quiescing": _parse_strict_bool,
    "teams_enabled": _parse_strict_bool,
    "teams_max_panes": _parse_overridable_positive_int(1),
    "teams_idle_minutes": _parse_overridable_positive_int(30),
    "teams_display": TeamsDisplay.parse,
    "require_human_approval_to_merge": _parse_strict_bool,
    "substrate_self_signoff": _parse_strict_bool,
    "substrate_auto_merge_authorized_by": _parse_strict_str,
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
    # #3201 PR-3b — the CI-eval self-heal autonomous-fixer OFF switch (DARK flag).
    "ci_eval_heal_autofix_enabled": _parse_strict_bool,
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
    "triage_assessor_enabled": _parse_strict_bool,
    "triage_assessor_cadence_hours": _parse_strict_int,
    "triage_assessor_max_issues_per_tick": _parse_strict_int,
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
    "T3_WORKER_QUIESCING": ("worker_quiescing", _parse_env_bool),
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
class _WorkspaceCoreSettings:
    """Workspace root + the core engagement / identity flags.

    A private in-file group base (see :class:`UserSettings`). Pure data-declaration —
    no behaviour — so the flat persisted schema is preserved by inheritance.
    """

    workspace_dir: Path = field(default_factory=lambda: Path.home() / "workspace")
    privacy: str = ""
    check_updates: bool = True
    # #256 Default-OFF teatree engagement. When false (the default) a fresh
    # Claude session does NOT auto-engage teatree — no skill auto-suggest, no
    # PreToolUse load-block, no loop scheduling — and SessionStart shows a
    # one-line how-to-start advisory instead. The owner flips it true to
    # auto-activate every session. DB-home (DB-home cutover): the cold
    # SessionStart / UserPromptSubmit hooks read it DB-ONLY pre-Django via the
    # Django-free ``cold_reader`` (``teatree_settings._cold_db_bool``) and the bash
    # ``statusline.sh._autoload_db_value`` (sqlite3 CLI); ``T3_AUTOLOAD`` env wins, a
    # ``[teatree] autoload`` TOML value is ignored on read. Explicitly calling
    # ``/teatree`` — or loading any ``t3:`` skill — engages teatree for the
    # session regardless of this default.
    autoload: bool = False
    timezone: str = ""
    contribute: bool = False
    excluded_skills: list[str] = field(default_factory=list)


@dataclass
class _ModeHarnessSettings:
    """Mode / autonomy + the two-layer agent-harness (runtime / transport / provider) selectors."""

    mode: Mode = Mode.INTERACTIVE
    autonomy: Autonomy = Autonomy.BABYSIT
    # The single LANE selector for loop-dispatched phase agents (those whose
    # (role, phase) has a registered phase sub-agent). ``interactive`` (default,
    # today's behaviour) dispatches them in-session via the ``/loop`` slot's
    # ``Agent`` tool; ``headless`` runs them via ``agents/headless.py`` behind the
    # two-layer ``agent_harness`` (transport) / ``agent_harness_provider``
    # (credential) pair (#2887). Per-overlay overridable; ``T3_AGENT_RUNTIME`` env
    # wins.
    agent_runtime: AgentRuntime = AgentRuntime.INTERACTIVE
    # Layer 1 of the two-layer harness config model (#2887): which in-process
    # TRANSPORT a headless run uses. Orthogonal to ``agent_runtime`` (which LANE —
    # interactive vs headless — a task dispatches into): once a run IS headless,
    # this picks the transport that opens the agent session behind the
    # ``teatree.agents.harness.Harness`` protocol. ``claude_sdk`` (default, today's
    # behaviour) is the ``claude-agent-sdk`` backend; ``pydantic_ai`` (#2885) is the
    # OrcaRouter-BYOK, OpenAI-compatible backend. The backend set is OPEN (#3157 E1):
    # this is a registry KEY, not a closed enum — an overlay registers a third
    # transport under the ``teatree.harnesses`` entry-point group and selects it here
    # by name (an unregistered name fails LOUD at dispatch, not at config parse). The
    # built-in ``AgentHarness`` values remain the two shipped keys. Per-overlay
    # overridable; ``T3_AGENT_HARNESS`` env wins.
    agent_harness: str = AgentHarness.CLAUDE_SDK
    # Layer 2 of the two-layer harness config model (#2887): the provider/
    # credential a headless run authenticates with, CONSTRAINED by Layer 1
    # (``AgentHarnessProvider.valid_for(agent_harness)`` — see the enum
    # docstring for the full constraint table). Default ``None`` — NO explicit
    # pin: a ``ClaudeSdkHarness`` dispatch inherits the ambient environment
    # unchanged (today's behaviour, and the legacy default the pre-#2887
    # ``agent_runtime=interactive``/``api`` fallthrough exercised), so an
    # operator who never touches this setting is never forced through an eager
    # credential lookup they haven't configured. An explicit ``subscription_oauth``
    # forces the plan's OAuth token (stripping the API key) — the ``claude_sdk``
    # default STANCE once pinned. ``api_key`` forces the metered key — the
    # ``claude_sdk``-only opt-in. ``orca_router_byok`` is the sole implemented
    # ``pydantic_ai`` provider today — ``PydanticAiHarness`` does not yet branch
    # on this field (there is only one option), so it ships wired for the
    # constraint table and a future Vertex binding rather than as an active
    # branch on that path. Per-overlay overridable; ``T3_AGENT_HARNESS_PROVIDER``
    # env wins.
    agent_harness_provider: AgentHarnessProvider | None = None
    # Whether this overlay's headless lane is the REGULATED path — carrying client/
    # bank data under EU data-residency & regulatory compliance (GDPR, data
    # residency, processor jurisdiction) (#2887). Default ``False``: the teatree
    # factory lane carries no regulated data, so it runs unrestricted (any model,
    # incl. cheap open-source ones). A regulated lane sets this ``True``,
    # restricting inference to the models on ``regulated_path_model_allowlist``.
    # Enforced by ``teatree.agents.model_tiering.assert_model_allowed_on_regulated_path``,
    # called from ``PydanticAiHarness`` before a resolved OrcaRouter model name is used
    # (CLIENT-SIDE, best-effort — the OrcaRouter dashboard Allowed-models glob is the
    # hard boundary). Per-overlay overridable; ``T3_ENFORCE_REGULATED_PATH`` env wins.
    enforce_regulated_path: bool = False
    # The EXPLICIT allowlist of model-id patterns eligible to run on the regulated
    # path (matched case-insensitively as substrings). A BYOK / residency-controlled
    # set the operator enumerates for their regulated lane; empty (the default) makes
    # nothing eligible, so a lane with ``enforce_regulated_path`` on and an empty
    # allowlist refuses every model (fail-closed). Inert while ``enforce_regulated_path``
    # is ``False`` (the teatree factory default). Per-overlay overridable.
    regulated_path_model_allowlist: list[str] = field(default_factory=list)
    # Per-run sequential-request cap for the ``pydantic_ai``/OrcaRouter harness
    # (OrcaRouter setup plan §4 guardrail #1). Passed as pydantic_ai
    # ``UsageLimits(request_limit=...)`` on every ``PydanticAiHarnessSession`` run
    # so a cheap-model maker cannot drift on a long tool loop — the FSM already
    # chunks work into phases and the orchestrator re-dispatches, so a tight
    # per-run cap composes with orchestration rather than killing tasks. Applies
    # ONLY to the ``pydantic_ai`` harness (the default ``claude_sdk`` harness is
    # bounded by the loop watchdog instead), so it is inert until an overlay opts
    # into ``agent_harness=pydantic_ai``. ``0`` disables the cap (the escape
    # hatch). Per-overlay overridable.
    pydantic_ai_request_limit: int = 5
    # The ``pass`` store path the OrcaRouter BYOK key is read from. The
    # ``OrcaRouterCredential`` has NO built-in default, so this is the ONLY ``pass``
    # source: an operator points teatree at an existing per-account ``pass`` entry
    # (e.g. ``orcarouter/<account>/api-key``) with no copy. Empty (the default) means
    # the credential resolves only from ``ORCA_ROUTER_API_KEY`` (which still wins over
    # ``pass``) and otherwise fails loud naming this setting. Per-overlay overridable.
    orca_router_pass_path: str = ""
    # The ``x-lane`` header value every ``pydantic_ai``/OrcaRouter request rides
    # (OrcaRouter setup plan §3.4), so the named router's analytics — and a future
    # DSL rule that keys on it — can tell the three call-site lanes apart:
    # ``factory`` (default — the headless factory dispatch), ``eval`` (the eval CI
    # job, set via ``T3_ORCA_ROUTER_LANE=eval`` in that job's env), and ``bulk``
    # (a secondary overlay's cheap bulk legs, set per-overlay). Informational under the shipped
    # ``gated_adaptive`` router until a DSL rule matches it; resolved SYNCHRONOUSLY in
    # ``resolve_harness`` and threaded through ``OrcaLaneConfig.lane``. Inert until an
    # overlay opts into ``agent_harness=pydantic_ai``. Per-overlay overridable;
    # ``T3_ORCA_ROUTER_LANE`` env wins.
    orca_router_lane: str = "factory"
    # The OrcaRouter router HANDLE (e.g. ``orcarouter/secondary-factory``) this overlay's
    # ``pydantic_ai`` dispatches resolve to — the ``teatree-factory`` vs secondary-router
    # two-router split, config/overlay-driven, not hardcoded. Empty (the default) falls
    # back to the ``PYDANTIC_AI_TIER_MODELS`` handle (``orcarouter/teatree-factory``).
    # Overrides ONLY the normalise-UP-to-handle branch of ``resolve_pydantic_ai_model``,
    # so an explicit Orca-native model pin still wins. Resolved SYNCHRONOUSLY in
    # ``resolve_harness`` and threaded through ``OrcaLaneConfig.router_name``. Inert
    # until an overlay opts into ``agent_harness=pydantic_ai``. Per-overlay overridable;
    # ``T3_ORCA_ROUTER_NAME`` env wins.
    orca_router_name: str = ""
    # Which Anthropic credential the automated eval lane (the metered ``api``
    # backend + the LLM judge) authenticates with. ``subscription_oauth`` (default,
    # reverses #2707) rides the plan's OAuth token — no per-token bill, but a
    # depleting usage window, so the CI lane is right-sized (single effort tier,
    # smaller trial count, per-account routing via ``anthropic_oauth_pass_paths``).
    # ``metered_api_key`` rides the metered key (per-token cost, no window) and stays
    # selectable. Per-overlay overridable; ``T3_EVAL_CREDENTIAL`` env wins over both.
    eval_credential: EvalCredential = EvalCredential.SUBSCRIPTION_OAUTH


@dataclass
class _LoopAndTeamsSettings:
    """WIP dial + loop cadence/runner + the agent-teams pane budget."""

    # How much new work a loop tick admits at once — the bounded-WIP dial. The
    # conservative ``MEDIUM`` baseline means NO orchestrator fan-out — only
    # the intrinsic loop + PR sweep + per-overlay ``max_concurrent_auto_starts``
    # provide throughput. ``slow`` caps to one impl worker; ``full`` arms the
    # /t3:wip loop; ``boost`` keeps ``boost_concurrency = N`` workers live,
    # refilling the pool each tick as workers exit. Orthogonal
    # to ``mode``/``autonomy`` (those gate *whether* a publish proceeds; this
    # governs *how many* threads run) and never relaxes a safety gate.
    # Per-overlay overridable; ``T3_WIP`` env wins over both.
    wip: Wip = Wip.MEDIUM
    # Loop tick interval in seconds (BLUEPRINT § 5.6). Default 12 minutes.
    loop_cadence_seconds: int = 720
    # #1796 / PR-28 — the loop-cadence kill-switch. Default ON: the singleton
    # `t3 worker` owns the tick cadence, draining the self-rescheduling loop-timer
    # chains, and the SessionStart supervisor keeps at-least-one worker alive. There
    # is NO fallback plane — the legacy native-`/loop` cron mirror was retired in
    # PR-28, so flipping this OFF is the instant runtime escape that STOPS the loops
    # entirely; the worker supervisor re-reads this flag every ~5s and stops the
    # executor pool on flip-off. The `default`-queue drain still runs under OFF via
    # the reactive drain loop, so OFF halts loop ticks without stranding queued
    # FSM/headless work.
    # DB-home (#1775): resolved from the `ConfigSetting` store (global + overlay
    # rows) + `T3_LOOP_RUNNER_ENABLED` env; a `[teatree]`/`[overlays.<name>]` TOML
    # value is ignored on read. Set via `config_setting set loop_runner_enabled`.
    # The worker_supervisor cold-read default is pinned equal to this by
    # `tests/config/test_worker_default_parity.py` so a fresh install spawns a worker.
    loop_runner_enabled: bool = True
    # The drain-then-deploy admission gate (rolling/zero-downtime deploy). Default
    # OFF: the worker admits new work normally. `t3 worker drain` flips it ON for the
    # deploy window so the claim/admission path admits ZERO new tasks — the CAS
    # `claim_next_pending` and the `_claimable_for_target` query both short-circuit —
    # while in-flight CLAIMED leases keep renewing and finish. It is READ only at the
    # claim chokepoint; it deliberately does NOT feed the worker supervisor's
    # `loop_runner_enabled` stop condition, so quiescing never stops the supervisor or
    # kills a live sub-agent. The FRESH worker's init clears it so admission resumes.
    # DB-home (#1775): resolved from the `ConfigSetting` store (global + overlay rows)
    # + `T3_WORKER_QUIESCING` env; a TOML value is ignored on read. Set via `t3 worker
    # drain` (which writes it) or `config_setting set worker_quiescing`.
    worker_quiescing: bool = False
    # #1838 Track-B PR#6 — the inert agent-teams WORK layer. When false (the
    # default, fail-OFF), the team-role registry (`teatree.teams.roles`) is
    # PURE DATA referenced by nothing in the loop/dispatch/claim path: the
    # WORK-team ships DARK. When flipped on, a LATER PR wires the
    # `team:<role>` claim namespace + the overlay-seam claim filters into a
    # pane-backed teammate; this PR adds only the config surface. DB-home
    # (#1775): resolved from the `ConfigSetting` store (global + overlay rows) +
    # `T3_TEAMS_ENABLED` env; a `[teams]`/`[overlays.<name>]` TOML value is ignored
    # on read. Set via `t3 teams on|off` (the DB-row write path).
    teams_enabled: bool = False
    # #1838 Track-B PR#7a — the inert maker-only pane budget. `teams_max_panes`
    # caps how many concurrent maker panes a lead may run; `teams_idle_minutes`
    # is the idle-pane reaper threshold (a pane with no live Session/Task past
    # this many minutes is demoted to stopped). Both ship inert with the rest of
    # the pane layer (referenced by nothing until `teams_enabled` flips on and a
    # consumer lands). DB-home (#1775): resolved from the `ConfigSetting` store
    # (global + overlay rows) + `T3_TEAMS_MAX_PANES` / `T3_TEAMS_IDLE_MINUTES`
    # env; a `[teams]`/`[overlays.<name>]` TOML value is ignored on read. A
    # non-positive or non-int value FAILS SAFE to the default at every tier — the
    # safety bound cannot be configured away by a typo.
    teams_max_panes: int = 1
    teams_idle_minutes: int = 30
    # #1838 Track-B WI-5 — the PRESENTATION-only pane-display mode. Governs
    # whether a maker pane's in-process SDK session is ALSO rendered in a visible
    # tmux pane (native iTerm2 split under `tmux -CC`, plain tmux pane elsewhere).
    # The SDK session is the source of truth; this never replaces it. Default
    # `none` (ships dark, byte-identical to today); `auto`/`tmux` opt into the
    # display with graceful degradation (no tmux / no TTY / spawn failure falls
    # back to the in-process path). DB-home (#1775): resolved from the
    # `ConfigSetting` store (global + overlay rows) + `T3_TEAMS_DISPLAY` env; a
    # `[teams]`/`[overlays.<name>]` TOML value is ignored on read. A bad value
    # fails SAFE to `none` at every tier.
    teams_display: TeamsDisplay = TeamsDisplay.NONE


@dataclass
class _OnBehalfSettings:
    """Human-approval training wheels + the on-behalf post gate + bot→user notify flags."""

    # Training-wheel for `auto` overlays: when true, the loop autonomously
    # pushes and creates PRs but stops short of merging — merge requires a
    # human reaction (👍 or `/merge`). The user flips this off only once
    # comfortable (BLUEPRINT § 5.6.2). No effect in `interactive` mode,
    # where every publishing action prompts regardless.
    require_human_approval_to_merge: bool = True
    # Whether the standing grant may sign off a SUBSTRATE merge (#3223). Default
    # off: a substrate CLEAR (merge keystone, architecture spec, governance doc,
    # self-guardrail seam) PINGS-and-HOLDS for the owner's per-PR sign-off even at
    # `autonomy = full` — the #2727 safety posture. Turning this on lets
    # `_overlay_grants_standing_substrate_signoff` cover a substrate CLEAR on an
    # overlay standing at `autonomy = full` (the solo-owned tier), so the owner's
    # own green PRs self-authorize substrate merges the same way non-substrate
    # clears already do. This changes only WHO authorizes the sign-off; the
    # quality/safety floor (independent cold review, reviewed-SHA bind, CI-green,
    # not-draft, maker≠checker, anti-vacuity) is untouched and still runs. The
    # `full` tier gate is kept so a below-full overlay never self-merges substrate
    # even with this on. DB-home (#1775), per-overlay overridable.
    substrate_self_signoff: bool = False
    # The owner id the headless loop presents as the standing substrate merge
    # authorization (#3413). Empty (the default) preserves the hold-for-owner
    # posture verbatim — a substrate CLEAR PINGS-and-HOLDS and is never
    # auto-merged (invariant 4). Setting it to an owner id is the durable,
    # revocable delegation: the config WRITE is the human authorization. When
    # set, the `pr_sweep` scanner presents this id at merge time as the
    # `--human-authorized` a substrate CLEAR requires, and the keystone
    # (`_config_standing_substrate_delegation`) authorizes the merge ONLY when the
    # presented id still equals this configured value — sourced from config, never
    # a live CLI flag, so unsetting it revokes the delegation at the next merge.
    # Every gate still runs (green required checks, recorded merge_safe verdict,
    # clean rebase, draft-lock, maker≠checker, SHA-bind); this changes only WHO
    # supplies the substrate authorization (a standing config delegation vs. a
    # per-PR recorded human approval), and the merge is audited as config-sourced
    # (`MergeAudit.standing_delegation_by`) to stay distinguishable from an
    # interactive human authorization. DB-home (#1775), per-overlay overridable.
    substrate_auto_merge_authorized_by: str = ""
    # Per-(repo, ticket) open-PR budget: the max number of concurrently-open
    # (not-merged) PRs a single ticket may have in one repo. Enforced at the
    # core PR-creation seam by ``pr_budget_gate`` before a PR is opened. The
    # shipped default is ``1`` — "at most one open PR per repo per ticket" — so
    # the one-ticket-one-PR discipline holds out of the box (a stray second PR
    # for the same ticket in the same repo is a recurring cleanup cost, D9). Set
    # ``0`` to restore the unlimited opt-out. Constraint-as-data: the scope is a
    # per-overlay ``ConfigSetting`` row, never a branch in core code, so an
    # overlay wanting a different value needs no code change. DB-home (#1775);
    # per-overlay overridable.
    max_open_prs_per_repo_per_ticket: int = 1
    # Training-wheel for the `t3:answerer` capability (#670, resolving
    # #654 Open Question #3): when true, the agent drafts a reply to an
    # inbound question, DMs the user for approval, and posts only on
    # confirmation. Set false to let the agent post answers directly — a
    # deliberate opt-in the user flips only once comfortable with answer
    # quality. Per-overlay overridable (a trusted overlay can opt into
    # direct posting without flipping the global). Default on, mirroring
    # `require_human_approval_to_merge`.
    require_human_approval_to_answer: bool = True
    # Tri-state pre-gate over on-behalf colleague/customer posts (#960):
    #
    # * ``DRAFT_OR_ASK`` (default) — colleague-invisible, revocable draft
    #   notes (``t3 review post-draft-note``) publish autonomously and
    #   the agent DMs the user with the publish/delete commands; every
    #   other gated action collapses to BLOCK identical to ``ASK``.
    # * ``ASK`` — every gated action requires an explicit recorded
    #   approval (``t3 review approve-on-behalf``) before it publishes.
    # * ``IMMEDIATE`` — the gate is off; gated actions publish directly
    #   (subject to the always-gated list in ``Mode``).
    #
    # DB-home (#1775): resolves from the ``ConfigSetting`` store + env only.
    # The pre-partition shim that translated a legacy ``[teatree]
    # ask_before_post_on_behalf`` TOML key into this mode is retired — that
    # TOML key is ignored on read now; migrate it with ``config_setting import``.
    # The default when no row is set is ``DRAFT_OR_ASK``.
    on_behalf_post_mode: OnBehalfPostMode = OnBehalfPostMode.DRAFT_OR_ASK
    # Carve-out from the on-behalf pre-gate: actions in this allowlist resolve
    # to PROCEED even under ASK / DRAFT_OR_ASK, because they are the user's
    # routine self-documentation on their OWN ticket (E2E evidence), not a
    # colleague-facing voice that needs the user's per-post approval. Default
    # includes ``post_e2e_evidence`` so the user never has to approve their own
    # evidence posts; clear the list (``on_behalf_auto_actions = []``) to
    # re-gate evidence under a blocking mode. Per-overlay overridable; env
    # ``T3_ON_BEHALF_AUTO_ACTIONS`` (comma-separated) wins over both.
    on_behalf_auto_actions: list[str] = field(default_factory=lambda: ["post_e2e_evidence"])
    # Whether agent-driven review-request posting is BLOCKED for this overlay
    # (#2579). Resolved off the autonomy TIER by ``_apply_autonomy``: the
    # ``notify`` tier (collaborative/customer surface) sets it ``True`` so
    # ``resolve_on_behalf_verdict("review_request_post")`` BLOCKs even though the
    # collapse forces ``on_behalf_post_mode = immediate``; the ``full`` tier (solo
    # tooling surface) leaves it ``False`` so review-request PROCEEDs; ``babysit``
    # keeps the default ``False`` and review-request follows ``on_behalf_post_mode``
    # like any other colleague-visible post. This is the customer-overlay
    # done-definition gate: an overlay running ``notify`` stops at "MR is mergeable
    # + review-requestable" and never auto-requests review. An explicit per-overlay
    # pin always wins over the tier (Option A — the per-overlay escape): a ``full``
    # overlay can pin ``True`` to suppress auto-request, and a ``notify`` overlay
    # can pin ``False`` to opt back in. Orthogonal to ``require_human_approval_to_merge``
    # (which gates merge, not the review-request post). Default off; per-overlay
    # overridable (DB-home).
    review_request_post_disabled: bool = False
    # Pass --chrome to every spawned `claude` session to attach the legacy
    # Claude-in-Chrome extension. Default OFF — chrome-devtools-mcp
    # (`chrome_devtools_mcp_enabled`) is the default browser tool now: it needs no
    # claude.ai account or extension pairing and covers navigation, interaction,
    # and inspection. Turn ON only to opt a host back into the Chrome extension.
    claude_chrome: bool = False
    # Whether the loopback admin dashboard auto-logs-in the first superuser
    # (`teatree.core.middleware.LocalAdminAutoLoginMiddleware`). Default ON so
    # `t3 admin` and the deploy's loopback admin need no password on their own
    # single-operator tool. This flag alone never opens the admin: the
    # middleware ALSO requires the request to originate from loopback
    # (`127.0.0.1` / `::1` / `INTERNAL_IPS`), so a non-loopback deployment is
    # ineffective even with the flag on — auto-login can never fire off-loopback.
    # DB-home, per-overlay overridable; set false to force Django's auth wall.
    admin_autologin_enabled: bool = True
    # Whether teatree should append an agent identity (`Co-Authored-By`,
    # "Sent using …", "Generated with …") to artifacts published on the
    # user's behalf — git commits, PR descriptions and comments, Slack
    # messages, issue bodies. Default off: the user is the author, the agent
    # is the typist. Honored by every teatree post-on-behalf code path; the
    # rule for ad-hoc agent posting (MCP Slack, gh comment, etc.) lives in
    # `skills/rules/SKILL.md` § "No AI Signature on Posts Made on the User's
    # Behalf".
    agent_signature: bool = False
    # Bot→user Slack notification channel (#963). When true, the helper
    # `teatree.core.notify.notify_user(...)` posts agent answers / questions /
    # important-info to the user's configured Slack DM via the bot identity,
    # auditing each send in the `BotPing` ledger. Out of scope of the
    # on-behalf gates (#960/#949): those govern posts the agent makes *as*
    # the user to colleagues/customers; this is the bot talking to its own
    # operator. Default on; turn off to keep notifications CLI-only.
    notify_user_via_bot: bool = True
    # After-receipt visibility DM (#949). When true (default), every
    # colleague-visible post the agent makes under the user's identity is
    # followed by a bot→user DM naming the destination, a clickable
    # artifact link, and a one-line summary — durable enforcement that
    # retires the per-session memory `notify-user-on-every-post-on-behalf`.
    # Distinct from the `on_behalf_post_mode` pre-gate (which decides
    # *whether* a post may publish): this fires *after* a successful
    # publish and never blocks or rolls back the post. DB-home: flip off via
    # `t3 <overlay> config_setting set notify_on_post_on_behalf false`
    # (a `[teatree] notify_on_post_on_behalf` TOML value is ignored on read);
    # per-overlay overridable; intentionally NO env var (notify_user_via_bot,
    # its sibling, has none — a copied-by-analogy env layer would be a lie).
    # Out of scope: internal orchestration writes (bot→user DMs, the
    # loop's own bookkeeping) — only colleague-visible on-behalf posts.
    notify_on_post_on_behalf: bool = True
    # Derived under the ``notify`` tier by ``_apply_autonomy``; ORed with the field above.
    notify_on_behalf: bool = False


@dataclass
class _IdentityRoutingSettings:
    """Statusline chain, operator identity aliases, repo mode, and the missing-issue policy."""

    statusline_chain: list[str] = field(default_factory=list)
    # Usernames / handles that all map to the same human operator across
    # platforms (a GitHub login, a GitLab username, an internal handle).
    # Two consumers:
    # - The ticket-disposition scanner uses them to suppress the reassign
    #   signal when an issue is handed off between two of the operator's
    #   own identities — plumbing noise, not an actionable transition
    #   (souliane/teatree#975).
    # - The loop's PR/MR scanners union-query each alias so cross-forge
    #   work (e.g. multiple GitHub logins under one PAT, GitHub vs GitLab
    #   handles for the same human) surfaces in the statusline (#976).
    # Default empty preserves legacy single-identity behaviour. Per-overlay
    # overridable so a tracker-scoped overlay can carry tracker-specific
    # handles without flipping the global default.
    user_identity_aliases: list[str] = field(default_factory=list)
    # Solo vs collaborative working mode (issue #550 item 4). Empty string
    # = auto-detect from `git shortlog` history (see teatree.repo_mode);
    # an explicit "solo" / "collaborative" pins the verdict and bypasses
    # detection. Consumed by skills via `t3 tool repo-mode` so the
    # ask-first-vs-fix-proactively decision lives in one place, not in
    # every skill.
    repo_mode: str = ""
    # What to do when a commit/MR needs an issue reference and the agent has
    # none. Default ``FIND_EXISTING_THEN_ASK``: always recover the ORIGINAL
    # existing issue first; if none is found, ASK the user on a colleague-
    # facing/external repo and CREATE on the user's own repo — never a dummy
    # ref. ``CREATE`` / ``DUMMY`` are opt-in tiers that authorise auto-create /
    # placeholder-ref on a colleague-facing repo too. Per-overlay overridable
    # via ``[overlays.<name>].missing_issue_ref_policy``; ``T3_MISSING_ISSUE_POLICY``
    # env wins over both. Resolved by
    # ``teatree.missing_issue_policy.resolve_missing_issue_verdict``; the agent
    # prose lives in ``skills/ship/SKILL.md`` § "Missing Issue Reference Policy".
    missing_issue_ref_policy: MissingIssuePolicy = MissingIssuePolicy.FIND_EXISTING_THEN_ASK


@dataclass
class _QualityGateSettings:
    """The architectural-review cadence + the opt-in DoD / merge / critic / send-proxy quality gates."""

    # #1136 / #1152 Periodic architectural-review scanner — CORE
    # always-on (not per-overlay opt-in). The cadence applies uniformly
    # to every overlay's worktrees because it is a teatree-platform
    # behaviour. Set ``architectural_review_disabled = true`` in
    # ``[teatree]`` (or per-overlay) as the escape hatch.
    architectural_review_disabled: bool = False
    architectural_review_skill: str = "ac-reviewing-codebase"
    architectural_review_cadence_hours: int = 168
    architectural_review_after_merge_count: int = 25
    # #1539 Per-ticket deep-review skill. Empty = opt-in unset: the
    # reviewing-phase evidence gate (``teatree.core.gates.review_skill_gate``) is
    # a NO-OP, so projects that do not configure a review skill keep
    # recording the ``reviewing`` attestation unchanged. When set (e.g.
    # ``ac-reviewing-codebase``), ``lifecycle visit-phase <id> reviewing``
    # refuses to record the phase without durable evidence the skill ran.
    # Distinct from ``architectural_review_skill`` (the periodic cadence
    # scanner) — this one gates a single ticket's reviewing attestation.
    review_skill: str = ""
    # Opt-in deep-retrieval gate on ``-> reviewing`` (``review_context_gate``);
    # default false = NO-OP. Per-overlay overridable.
    require_review_context: bool = False
    # #1829 Opt-in SHA-bound anti-vacuity gate on review-request/merge
    # (``anti_vacuity_gate``); default false = NO-OP. Per-overlay overridable.
    require_anti_vacuity_attestation: bool = False
    # PR-08 Opt-in review-state gate on the review-request broadcast
    # (``review_request_state_gate``): a broadcast is refused unless the ticket
    # is REVIEWED with a recorded review-evidence artifact (a ``ReviewEvidence``
    # cold-review row or a ``ReviewVerdict`` from the cold-review step). Default
    # false = NO-OP so a normal reviewed-and-cleared flow is never blocked.
    # Per-overlay overridable.
    require_reviewed_state_for_review_request: bool = False
    # PR-08 Opt-in cross-repo integration-review DoD gate on ``mark_delivered``
    # (``integration_review_gate``): a ticket touching ≥ 2 repos cannot reach
    # DELIVERED without an integration-review ``ReviewEvidence`` covering the
    # combined changeset. A single-repo ticket never trips it. Default false =
    # NO-OP. Per-overlay overridable.
    require_integration_review: bool = False
    # #4a Opt-in merge-evidence FSM gate on ``mark_merged`` / ``reconcile_merged``
    # (``merge_evidence_gate``): the terminal MERGED state is unreachable without
    # real merged-SHA evidence — a keystone ``MergeAudit`` row OR the forge itself
    # confirming the PR merged (fail-closed live probe). Kills "believe work is
    # done when it's not" at the FSM root: the ungated ``_advance_ticket`` walk
    # can no longer mark an unpushed/unmerged ticket done. Default false = NO-OP so
    # the generic FSM never blocks; flipped ON for the teatree overlay so it bites
    # real teatree tickets. Its OWN kill-switch (never another gate's) — setting it
    # back off is the operator's audited escape if a forge outage would otherwise
    # wedge a genuinely-merged ticket the forge cannot confirm. Per-overlay overridable.
    require_merge_evidence: bool = False
    # SELFCATCH-3 Opt-in plan-adequacy + late-bound-plan gate on ``code()`` /
    # ``schedule_coding`` (``plan_currency_gate``): coding is unreachable without an
    # ADEQUATE plan (a complete four-section manifest — design, integration_seams,
    # edge_cases, test_strategy — each substantive OR an explicit reasoned negative)
    # that is BOUND to the current target HEAD (a plan whose base_sha moved and whose
    # intervening commits touch a declared seam is treated ABSENT — stale-is-absent).
    # Forecloses the named root cause of the 26-bug integration campaign:
    # thin-spec-as-plan and stale-base coding. Also flips ``PlanArtifact.record()``
    # strict — a new row needs a 40-char base_sha + complete manifest. Default false =
    # NO-OP so the generic FSM never blocks; the operator flips it ON per-overlay
    # (``config_setting set require_plan_adequacy true --overlay <name>``) once the
    # planner produces manifests. Its OWN kill-switch (setting it back off) is the
    # audited never-lockout escape alongside ``plan-reaffirm``. A feature flag
    # (governed in ``FEATURE_FLAGS``). Per-overlay overridable.
    require_plan_adequacy: bool = False
    # #118 Opt-in forced-repro gate on ``ship()`` for FIX-kind tickets
    # (``repro_gate``): a fix cannot ship without a harness-recorded, provenance-
    # verified RED->GREEN reproduction — a failing command captured against the
    # pre-fix tree (``merge-base --is-ancestor red green`` with ``red != green``),
    # then the SAME command passing once the fix is applied. The harness runs both
    # commands and stamps both SHAs, so exit codes and provenance cannot be forged
    # in prose. A genuinely repro-less failure (race/heisenbug) is unblocked by a
    # HUMAN-authorized ``ReproWaiver`` (maker != checker — the agent can never
    # self-waive). Default false = NO-OP so the generic ship chain never blocks;
    # the operator flips it ON per-overlay
    # (``config_setting set require_executed_repro true --overlay <name>``). Its OWN
    # kill-switch (setting it back false) is the audited never-lockout escape. A
    # feature flag (governed in ``FEATURE_FLAGS``). Per-overlay overridable.
    require_executed_repro: bool = False
    # North-star PR-3 The deterministic no-new-tech-debt MERGE gate on ``pr create``
    # (``debt_delta_gate`` in ``_run_ship_gates``): a ship diff that introduces
    # NET-NEW debt — a new ``noqa`` / ``type-ignore`` / ``pragma-no-cover`` comment,
    # an unreferenced ``pytest.mark.skip`` / ``xfail``, a new ``per-file-ignores``
    # entry, or a lowered ``fail_under`` coverage floor — is refused unless the plan
    # manifest records an ``approved_debt`` waiver naming the pattern + reason.
    # Delta, not absolute: only diff-ADDED lines are scanned, so pre-existing debt
    # is never flagged and removing debt is always allowed (the shrink-only ratchet).
    # Mechanizes CLAUDE.md's "no tech debt without explicit approval" — the approval
    # becomes a recorded, audited artifact. Default false = NO-OP so the generic ship
    # chain never blocks; the operator flips it ON per-overlay
    # (``config_setting set require_debt_delta true --overlay <name>``). Its OWN
    # kill-switch (setting it back false) is the audited never-lockout escape. A
    # feature flag (governed in ``FEATURE_FLAGS``). Per-overlay overridable.
    require_debt_delta: bool = False
    # north-star PR-4 The merge-quality critic's ENFORCEMENT switch for ORDINARY
    # tickets on the keystone merge precondition (``merge_quality_gate``): a
    # ``transition="merge"`` ``CriticVerdict`` (``test_value`` + ``cleanliness``)
    # covering the exact shipped head must exist and carry zero FAILs, or the merge
    # is refused. DIRECTIVE tickets are held to this bar UNCONDITIONALLY (self-
    # modification gets no benefit of the doubt) — this flag governs only whether
    # ORDINARY tickets are gated too. Default false = NO-OP for ordinary tickets:
    # they merge unchanged. Flip true per-overlay once the merge critic has proven
    # non-vacuous. Its OWN kill-switch (setting it back false) is the audited
    # never-lockout escape. A feature flag (governed in ``FEATURE_FLAGS``).
    # Per-overlay overridable.
    require_merge_quality_verdict: bool = False
    # The branch-protection required-status-check contexts the operator KNOWS must
    # gate a merge on this overlay's repos (e.g. ``["test (3.13)"]``). A fail-closed
    # floor: when the forge reports a DETERMINATE-EMPTY required set (branch
    # protection removed or never configured) while this floor is non-empty, the
    # keystone CI verdict fails closed to ``failed`` — a removed branch-protection
    # gate can no longer classify as "all checks passed / green". Default empty =
    # NO-OP (a genuinely gate-less repo still merges); the operator opts in per
    # overlay once their repos carry required checks. Per-overlay overridable.
    expected_required_contexts: list[str] = field(default_factory=list)
    # SELFCATCH-5 / #104 The autonomous user-proxy critic's ENFORCEMENT posture on
    # ``mark_delivered`` (``critic_gate``), re-typed from the former boolean
    # enforcement flag. The critic ALWAYS records the cheap deterministic
    # ``CriticFinding`` per failing rubric item; this tri-state decides whether the
    # EXPENSIVE async LLM critic is armed and whether a blocking finding refuses the
    # delivery. ``off`` (default) = dark: no async dispatch, no block. ``advisory`` =
    # arm the async critic + record ``CriticVerdict`` rows, never raise (the mode that
    # accumulates critic-liveness evidence pre-enablement). ``blocking`` = arm + refuse
    # the delivery on a blocking deterministic finding (fail-closed, the ticket stays
    # RETROSPECTED). Setting it back to ``advisory`` (recording continues) is the
    # audited never-lockout escape. A feature flag (governed in ``FEATURE_FLAGS``).
    # Per-overlay overridable.
    critic_gate_mode: CriticGateMode = CriticGateMode.OFF
    # #117 send-proxy — every outbound artifact (Slack post/DM/react, forge
    # PR/MR/issue comment) routes through ``teatree.core.send_proxy``, which
    # redaction-scans the payload and checks the destination against
    # ``send_proxy_allowlist``. ``send_proxy_mode`` is the enforcement posture:
    # ``warn`` (default, audit-only — records a ``SendAudit`` row, never blocks,
    # never mutates the live payload) accumulates the destination soak; ``enforce``
    # deterministically refuses a non-allowlisted destination and redacts the
    # payload. Flip to ``enforce`` only after seeding the allowlist from a WARN
    # soak. A feature flag (governed in ``FEATURE_FLAGS``). Per-overlay overridable.
    send_proxy_mode: SendProxyMode = SendProxyMode.WARN
    # The per-overlay destination allowlist the send-proxy checks in ``enforce``
    # mode: ``fnmatch`` globs over the raw destination (Slack channel id, ``org/repo``
    # slug, forge host) and the channel-qualified ``<channel>:<destination>`` form.
    # Empty by default; seeded from the WARN-soak's ``SendAudit`` destinations before
    # any enforce flip. The user's own DM is always allowed (never-lockout carve-out),
    # so an empty allowlist can never gate the bot→user notify path. Per-overlay overridable.
    send_proxy_allowlist: list[str] = field(default_factory=list)
    # PR-08 No-bulk-close threshold: a single command/agent action closing more
    # than this many tickets/MRs is refused without an explicit per-item
    # confirmation token (``bulk_close_gate``). A close of ≤ threshold items is
    # always allowed. Per-overlay overridable.
    bulk_close_threshold: int = 5
    # #2241 Opt-in rubric->verifier done-gate on the keystone merge precondition
    # (``rubric_gate``): the ticket's rubric of acceptance criteria must be fully
    # PASS by an independent verifier (grader != maker) at the merge-time head
    # SHA. Default false = NO-OP. Per-overlay overridable.
    require_rubric_verification: bool = False
    # #2232 Opt-in per-ticket spec-coverage DoD gate on ``mark_delivered``
    # (``spec_coverage_gate``): when on, a ticket cannot reach DELIVERED unless
    # every acceptance criterion in ``extra['spec_coverage']`` has a backing
    # test — done cannot be declared on a partial subset. Default false = NO-OP.
    # Per-overlay overridable.
    require_spec_coverage: bool = False
    # E2E confidence threshold (0-100): the rubric score a Playwright spec must
    # reach to be VERIFIED by the verify<->review loop. The single knob both the
    # `e2e-review` rubric (`/t3:e2e-review` § "E2E Confidence Rubric") and the
    # `e2e` loop (`/t3:e2e` § "Verify-Review Loop to Threshold") read, so "the
    # threshold" is one resolved value. Default 90; a stricter client overlay
    # raises it, a fast dogfood overlay lowers it. Documentation-only knob today
    # (the loop is agent-driven prose, not a deterministic gate) — this field is
    # the typed home so the doc value and any future programmatic consumer share
    # one source of truth. Per-overlay overridable.
    e2e_confidence_threshold: int = 90


@dataclass
class _ScannerSettings:
    """The periodic loop scanners — news, local-eval, backlog-sweep, dogfood-smoke, self-update cadences."""

    # #1191 Periodic scanning-news scanner — CORE always-on with a daily
    # cadence (24h). Companion to the `scanning-news` skill (#1190): the
    # loop fires a `scanning_news` task daily so the news-scan workflow
    # runs without depending on an external cron. Set
    # ``scanning_news_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch.
    scanning_news_disabled: bool = False
    scanning_news_skill: str = "scanning-news"
    scanning_news_cadence_hours: int = 24
    # #1391 Ask-gate for news-scan ticket creation. When true (default),
    # the scanning-news skill must NOT auto-create issues — it records a
    # ``PendingArticleSuggestion`` per candidate and surfaces the batch
    # to the user, filing an issue only on explicit approval. Default ON:
    # backlog pollution from unconfirmed auto-filing is the failure mode
    # this gate forecloses. Per-overlay overridable.
    ask_before_creating_news_tickets: bool = True
    # Periodic local-eval scanner — CORE always-on with a weekly cadence
    # (168h). User directive (2026-06-05): "AI evals should be run locally
    # from time to time, and in CI once a week." The loop fires an
    # ``eval_local`` task per cadence window so the SCOPED eval suite runs
    # locally via the no-API-key subscription runner (the same path
    # ``t3 eval run`` defaults to), without depending on an external cron.
    # Set ``eval_local_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch.
    eval_local_disabled: bool = False
    eval_local_skill: str = "eval"
    eval_local_cadence_hours: int = 168
    # #2419 Periodic backlog-sweep scanner — DEFAULT-OFF (kill switch ships
    # ON) with a weekly cadence (168h). Companion to the `sweeping-tickets`
    # skill: once the sweep's verdicts prove trustworthy the loop fires a
    # low-frequency `backlog_sweep` task that consolidates the issue tracker
    # (shipped / consolidate-into-epic / regressive / still-standalone
    # against current `main`). The sweep is destructive-capable — it can
    # propose closing issues — so unlike the always-on news/eval scanners
    # the kill switch defaults ON: the scanner stays inert until the user
    # sets ``backlog_sweep_disabled = false`` in ``[teatree]`` (or
    # per-overlay).
    backlog_sweep_disabled: bool = True
    backlog_sweep_skill: str = "sweeping-tickets"
    backlog_sweep_cadence_hours: int = 168
    # #2419 Ask-gate for backlog-sweep issue closes. When true (default),
    # the sweeping-tickets skill must NOT mass-close or mass-fold issues
    # unattended — it records each close proposal with its citation and
    # surfaces the batch to the user, closing only on explicit approval.
    # Only the high-confidence shipped-by-merged-PR class auto-closes.
    # Default ON: an unattended wrong close destroys tracker signal, the
    # failure mode this gate forecloses. Per-overlay overridable.
    ask_before_backlog_sweep_closes: bool = True
    # #1308 Periodic provision-smoke scanner — CORE always-on with a
    # 24h cadence by default. Queues a ``dogfood_smoke`` task per cadence
    # window so the loop exercises the active overlay's provision path
    # before the user reaches for E2E. Set
    # ``dogfood_smoke_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch. ``dogfood_smoke_overlay`` pins which overlay
    # anchor the placeholder task is created against — empty falls back
    # to the active overlay resolved via ``discover_active_overlay``.
    dogfood_smoke_disabled: bool = False
    dogfood_smoke_skill: str = "dogfood-smoke"
    dogfood_smoke_cadence_hours: int = 24
    dogfood_smoke_overlay: str = ""
    # #1249 Auto t3-update scanner — fast-forwards the editable teatree
    # clone + every registered overlay clone to ``origin/<default>`` once
    # the cadence has elapsed. Hourly default keeps the orchestrator
    # current without spamming the upstream remote on every tick. Set
    # ``self_update_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch.
    self_update_disabled: bool = False
    self_update_cadence_hours: int = 1
    # ``T3_LOOP_AUTO_UPDATE`` env overrides ``auto_update_reinstall``;
    # ``auto_update_require_green_main`` fails closed on non-green default-branch CI.
    auto_update_reinstall: bool = False
    auto_update_require_green_main: bool = True


# The regenerable cache dirs auto-purged at CRITICAL disk pressure (the
# ``disk_cache_allowlist`` default). A module constant so the field default stays a
# single line; ``.copy`` gives each settings instance its own list.
_DEFAULT_DISK_CACHE_ALLOWLIST = ["~/.cache/pre-commit", "~/.cache/puppeteer", "~/.cache/codex-runtimes"]


@dataclass
class _ResourcePressureSettings:
    """Resource-pressure auto-free thresholds (disk / RAM) + the destructive-lever opt-ins + task-sweep."""

    # #128 Resource-pressure scanner — teatree-controlled auto-free before
    # the host hits OOM / full-disk. Measures ABSOLUTE free bytes
    # (``os.statvfs`` for disk, ``vm_stat`` reclaimable pages for RAM) — never
    # percent-of-nominal (the APFS shared-container total and macOS "99 % RAM
    # used" both mislead). Monitoring + regenerable-cache purge are on by
    # default; every irreversible lever (worktree GC, process SIGTERM) is
    # flag-gated OFF. ``resource_pressure_disabled = true`` is the durable
    # kill-switch (mirrors ``self_update_disabled``): the scanner is never
    # wired. All knobs are per-overlay overridable.
    resource_pressure_disabled: bool = False
    resource_pressure_cadence_minutes: int = 5
    resource_pressure_min_free_interval_minutes: int = 30
    disk_warn_free_gb: float = 25.0
    disk_crit_free_gb: float = 10.0
    ram_warn_avail_gb: float = 3.0
    ram_crit_avail_gb: float = 1.5
    # Allow-LIST only (never a denylist): exactly these regenerable cache dirs
    # are auto-purged at CRITICAL. ``uv`` is handled via ``uv cache prune``.
    # ``~/.cache/prek`` and ``~/.claude/projects`` are deliberately absent —
    # the latter is hard-protected even if a user adds it.
    disk_cache_allowlist: list[str] = field(default_factory=_DEFAULT_DISK_CACHE_ALLOWLIST.copy)
    # Opt-in: enables stale-worktree GC (clean + fully pushed + unmodified
    # ``worktree_stale_days``) at CRITICAL, capped at
    # ``max_worktree_gc_per_tick`` per pass and never the active session's
    # worktree. Always logged + DM.
    allow_destructive_disk: bool = False
    worktree_stale_days: int = 30
    max_worktree_gc_per_tick: int = 3
    # Opt-in: enables SIGTERM (never SIGKILL) of allow-listed renderer
    # processes after >= 2 consecutive CRITICAL-RAM ticks, never a process in
    # the active-session ancestry. Empty ``ram_kill_allowlist`` means no
    # process is ever killed even when ``allow_destructive_ram = true``.
    allow_destructive_ram: bool = False
    ram_kill_allowlist: list[str] = field(default_factory=list)
    # #129 task-sweep scanner — per-overlay; verifies open teatree Task rows
    # against their artifact's terminal state (issue closed / PR merged) and
    # completes only on durable proof, never in bulk and never on a stale read.
    # On by default; ``task_sweep_disabled = true`` is the escape hatch.
    # ``task_sweep_recheck_interval_hours`` is the per-task anti-thrash window
    # (a task swept within it is skipped this tick) and the idempotency window
    # for the atomic ``last_sweep_check_ts`` stamp. Pre-rename, these keys were
    # ``todo_sweep_*``; a stored row under the old name still resolves via the
    # backward-compat alias in ``config/resolution.py``.
    task_sweep_disabled: bool = False
    task_sweep_recheck_interval_hours: int = 1
    # #1397 Cap on concurrent locally-running stacks for a single overlay.
    # Each running worktree (``services_up``/``ready``) holds docker
    # containers, browsers, language servers, and CI processes — on a
    # memory-constrained host (one OOM observed 2026-05-27 when two stacks
    # ran in parallel), one stack at a time is the workable limit. The
    # ``t3 <overlay> worktree start`` / ``workspace start`` gate refuses to
    # advance a second stack into ``SERVICES_UP`` while another is already
    # there, naming the blockers and pointing at ``worktree teardown``.
    # Default ``1``: for unattended 24/7 headless operation a single in-flight
    # local stack avoids merge conflicts against ``main`` and conserves the
    # Anthropic 5h token window. Set ``0`` to restore the legacy unbounded
    # behaviour, or any higher positive integer to raise the cap. Per-overlay
    # overridable: a heavy overlay can cap to ``1`` while a cheap dogfood
    # overlay stays unbounded (``0``).
    max_concurrent_local_stacks: int = 1


@dataclass
class _ProvisioningSettings:
    """Provisioning timeouts / concurrency + the idle-stack, stale-stack, and queue reaper knobs."""

    # #2220 Hard ceiling (seconds) for one long-blocking provisioning subprocess
    # — a DSLR snapshot restore, ``migrate``, or a ``--create-db`` test-DB
    # rebuild. On exceeding it the step ABORTS with an actionable error AND
    # fires a loud out-of-band user alert, instead of grinding silently for an
    # hour (the recurring "frozen sub-agent" symptom, e.g. a forked migration
    # graph). The default is generous (30 min) so a healthy restore+migrate
    # never trips it; a forked graph or a true hang blows past it and gets
    # aborted+alerted. A non-positive value degrades to the default — the
    # "never hang" invariant cannot be configured away. Per-overlay overridable.
    provision_step_timeout_seconds: int = 1800
    # Parallel ticket-workspace provisioning speed + resource-aware admission.
    # A fast step (symlinks, settings, a compose override) defaults to this
    # short ceiling; only a step explicitly marked ``ProvisionStep.heavy``
    # (a DB import, a frontend build) keeps ``provision_step_timeout_seconds``.
    # Per-overlay overridable.
    provision_fast_step_timeout_seconds: int = 120
    # nCPU-derived default concurrency cap for the bounded subprocess pool
    # ``workspace provision`` runs worktrees under. ``0`` (the default)
    # auto-derives from the host's ``os.cpu_count()`` at each read
    # (:func:`teatree.utils.ram_probe.default_provision_concurrency`); a
    # positive value pins an explicit cap. Per-overlay overridable.
    provision_max_concurrency: int = 0
    # RAM-used-percent ceiling above which a new provision is HELD (queued,
    # not started) rather than admitted, so a cold multi-repo provision never
    # pushes the host into OOM. Mirrors the self-improve budget gate's
    # ``DEFAULT_RAM_USED_CEILING_PCT``. Per-overlay overridable.
    provision_ram_ceiling_percent: int = 85
    # A provision whose total duration exceeds this many seconds fires a
    # best-effort out-of-band user alert — a regression in provisioning speed
    # must never be silently absorbed. Per-overlay overridable.
    provision_slow_threshold_seconds: int = 600
    # Reference-DB DSLR snapshots older than this many days are STALE; the
    # snapshot-warmer loop refreshes them out-of-band so a ticket-critical-path
    # provision never has to. Per-overlay overridable.
    snapshot_warmer_max_age_days: int = 1
    # On by default; ``snapshot_warmer_disabled = true`` is the escape hatch.
    snapshot_warmer_disabled: bool = False
    # #2190 Idle-stack reaper — a loop scanner that stops the docker stack of
    # an IDLE locally-running worktree (``services_up``/``ready``) and demotes
    # it to ``provisioned`` (REVERSIBLE: DB + worktree preserved), freeing the
    # host's RAM and a ``max_concurrent_local_stacks`` slot. Idle = no active
    # session/task on the ticket AND ``last_used_at`` older than
    # ``idle_stack_idle_minutes`` AND not the currently-active worktree AND no
    # active-delivery lease / recent E2E run / explicit pin (#2227).
    # Fail-safe: uncertainty ⇒ KEEP. On by default;
    # ``idle_stack_reaper_disabled = true`` is the escape hatch. All knobs are
    # per-overlay overridable.
    idle_stack_reaper_disabled: bool = False
    idle_stack_idle_minutes: int = 30
    idle_stack_reaper_cadence_minutes: int = 5
    # #2227 Recency window for the E2E-run KEEP guard: a worktree whose
    # ``Worktree.last_e2e_run`` is within this many minutes is the live target of
    # in-flight evidence work and is never reaped, even when otherwise idle.
    idle_stack_e2e_recent_minutes: int = 60
    # #2207 Stale-stack reaper — tears down docker compose stacks that NO
    # Worktree row owns (hand-rolled test stacks, failed-teardown leftovers)
    # once their newest container lifecycle event (created/started/finished)
    # is older than this many minutes. Age-keyed so a parallel session's
    # fresh manual stack is never reaped; an unknown age fails safe (keep).
    # Runs automatically before ``worktree start`` / ``workspace start`` /
    # ``workspace provision`` and on demand via
    # ``t3 <overlay> workspace reap-stale``. Default ``0`` keeps the sweep
    # OPT-IN (mirroring ``max_concurrent_local_stacks``): a positive value
    # (e.g. ``240``) enables it. Opt-in also keeps the suite hermetic — a
    # default-on sweep would let unit tests of start/provision reach the
    # developer's real docker daemon. Per-overlay overridable.
    stale_stack_min_age_minutes: int = 0
    # #2190/#44 Acquisition queue — when ``worktree start`` / ``workspace
    # start`` hits the cap, it reaps idle, retries, then ENQUEUES (no
    # SystemExit). A loop scanner drains the queue each tick with a
    # Fibonacci-minute backoff, never tearing down another ticket's stack.
    # On by default; ``local_stack_queue_disabled = true`` is the escape hatch.
    # ``local_stack_queue_max_attempts`` caps the Fibonacci retries before a
    # queued request is marked DEAD and surfaced.
    local_stack_queue_disabled: bool = False
    local_stack_queue_max_attempts: int = 13
    # fnmatch globs of branch names ``clean-all`` must NEVER reap even when the
    # squash-merge classifier says shipped — never-merge dev overrides, long-lived
    # spikes. Matched against the full branch name. Default empty: nothing
    # protected beyond the data-loss guards. Per-overlay overridable.
    clean_ignore: list[str] = field(default_factory=list)


@dataclass
class _PrePublishGateSettings:
    """Slack voice + speak/mr-reminder + the pre-publish / commit-time gate kill-switches and repo patterns."""

    # #1395 Slack voice/token mismatch classifier. The pre-publish gate
    # between ``chat.postMessage`` and the Slack API refuses (or warns)
    # when the body's voice ("PR merged" / "evidence" → agent vs "please
    # review" / "RR for" → user) and the token kind it would go out under
    # (``xoxp-`` = user, ``xoxb-`` = bot) disagree on a confident case
    # (the recurrence: agent-voice DM via the personal token to the user's
    # own DM channel, which Slack does not notify on). ``warn`` is the
    # backward-compat default — log the mismatch but allow the post;
    # ``strict`` raises ``SlackVoiceMismatchError`` and refuses the post;
    # ``off`` disables the classifier entirely.
    slack_voice_classifier_mode: SlackVoiceClassifierMode = SlackVoiceClassifierMode.WARN
    # #2060 The resolved speak config — a local playback enum (off/dm/all) + a
    # slack bool. DB-home (#1775, DB-home cutover): stored as a JSON dict
    # ConfigSetting (``parse_speak_setting``), rebuilt bespoke by the resolver; the
    # cold Stop hook reads it via ``cold_reader``. See :class:`SpeakConfig`.
    speak: SpeakConfig = field(default_factory=SpeakConfig)
    # The resolved slug→channel routing table for the cross-repo "my open MRs"
    # reminder; empty default keeps it inert. DB-home (#1775): stored as a JSON dict
    # ConfigSetting (``parse_mr_reminder_setting``), rebuilt bespoke by the resolver.
    mr_reminder: MrReminderConfig = field(default_factory=MrReminderConfig)
    # #1398 Pre-publish close-trailer scanner. fnmatch patterns over
    # ``namespace/repo``: when an MR/PR target repo matches one of these
    # patterns and the body carries a ``Closes|Fixes|Resolves`` trailer,
    # the trailer line is silently stripped before publishing. Default
    # empty preserves legacy behaviour. DB-home (#1775); set via
    # ``t3 <overlay> config_setting set ban_close_trailers_on_namespaces``;
    # the TOML value is ignored on read.
    ban_close_trailers_on_namespaces: list[str] = field(default_factory=list)
    # Pull-main-clone scanner — fast-forwards each work-repo *main clone*
    # under ``$T3_WORKSPACE_DIR`` to ``origin/<default>`` once the cadence
    # has elapsed, so a clone never drifts behind after a merge and
    # poisons ``git show`` / ``grep`` investigations. Hourly default keeps
    # the clones current without spamming each work repo's remote on every
    # tick. Set ``pull_main_clone_disabled = true`` in ``[teatree]`` (or
    # per-overlay) as the escape hatch.
    pull_main_clone_disabled: bool = False
    pull_main_clone_cadence_hours: int = 1
    # Fibonacci review-channel nag scanner (#1038). Ships DISABLED: a
    # concurrent-tick race on ``ReviewRequestPost.last_nag_step`` let two
    # sessions double-post bump replies into the colleague review channel,
    # including against already-merged MRs. Re-enable per-overlay via
    # ``[overlays.<name>].review_nag_enabled = true`` only after the
    # concurrency + merged-MR fixes are validated.
    review_nag_enabled: bool = False
    # Live-Slack dedup window for the review-request guard (#1084 follow-up).
    # The guard reads the review channel's recent history bounded to this many
    # days when deciding POST vs SUPPRESS; a posted ``ReviewRequestPost`` row is
    # NOT trusted on its own beyond this window — the guard live-verifies the
    # exact thread. Default 30 days (>= the previous hard-coded 24h) so live
    # Slack, not the DB row's age, decides. Fail-safe positive int: a
    # non-positive / mistyped value degrades to 30. Per-overlay overridable.
    review_request_dedup_window_days: int = 30
    # Channel-scan page cap for the review-request live dedup read (#3292 part 4).
    # The guard pages ``conversations.history`` up to this many times when
    # deciding POST vs SUPPRESS; the old hard-coded 5 could leave a ~30-day
    # window unreachable on a busy channel, so an old MANUAL user post fell
    # outside the scan and the request was duplicated. Fail-safe positive int:
    # a non-positive / mistyped value degrades to 5. Per-overlay overridable.
    review_request_dedup_max_pages: int = 5
    # Orchestrator-execution-boundary gate (#115, §17.6 gate 2). When
    # enabled (default), the main agent is blocked from running a HEAVY /
    # long-running foreground Bash command (test suite, build, dev
    # server, long sleep, full-tree sweep); ``run_in_background: true`` is
    # the escape hatch and sub-agents are unrestricted. The one-line
    # kill-switch ``[teatree] orchestrator_bash_gate_enabled = false``
    # disables the gate entirely (also read directly by the hook layer's
    # ``_orchestrator_bash_gate_enabled`` so a `t3 update` that reinstalls
    # the gate stays off until the user flips it back).
    orchestrator_bash_gate_enabled: bool = True
    # Mandatory-E2E FSM gate for customer-display-impacting changes (#1967).
    # When enabled (default), `pr create` and the §17.4 `ticket clear` refuse a
    # change the active overlay classifies as customer-display-impacting unless
    # recorded green E2E evidence exists at the reviewed tree OR a single-use
    # user-recorded `E2EBypassApproval` exists. Its OWN kill-switch — never a
    # reuse of another gate's switch: `[teatree] e2e_mandatory_gate_enabled =
    # false` (per-overlay overridable via `[overlays.<name>]`) disables it
    # entirely. The bypass is satisfiable per-tree only by the human user; a
    # maker/coding-agent/loop approver id is refused (maker≠checker).
    e2e_mandatory_gate_enabled: bool = True
    # Pre-flight attachment-fetch gate (PR-15, M5). When enabled (default), the
    # intake FSM step refuses to hand a ticket to the planner while any
    # attachment the ticket references (a GitLab upload, a linked Notion file, a
    # Slack-thread file) is still un-fetched under `<ticket_dir>/.attachments/`.
    # Its OWN kill-switch — `[teatree] attachment_gate_enabled = false`
    # (per-overlay overridable, DB-first) — lifts the hold so a stuck ticket is
    # never a lockout; the operator otherwise clears it with
    # `t3 <overlay> ticket attachments <ref> --fetch`.
    attachment_gate_enabled: bool = True
    # Snapshot-baseline pre-commit gate (§17.6). When enabled (default), a
    # commit that stages a Playwright visual baseline (a file under
    # `__snapshots__/` / `<spec>-snapshots/`) is refused unless the ticket
    # carries a green + POSTED `E2eMandatoryRun`. Its OWN kill-switch —
    # `snapshot_baseline_gate_enabled` (per-overlay overridable, DB-first) —
    # disables the hook; the reader (`scripts/hooks/check_snapshot_baseline.py`)
    # resolves it through `get_effective_settings`, so a DB `config_setting set`
    # actuates it exactly like the other gates.
    snapshot_baseline_gate_enabled: bool = True
    # Anti-relaxation + tach-soundness pre-commit gate (§17.6.1/§17.6.2, #850).
    # When enabled (default), the `gate-relaxation` prek hook
    # (`scripts/hooks/check_gate_relaxation.py`) refuses a commit whose staged diff
    # relaxes a lint/coverage constraint or a tach boundary. Its OWN kill-switch —
    # `gate_relaxation_gate_enabled` (per-overlay overridable, DB-first) — disables
    # the hook; the reader resolves it through `get_effective_settings`, so a DB
    # `config_setting set` actuates it exactly like the sibling gates.
    gate_relaxation_gate_enabled: bool = True
    # #122 Safety-biased incremental push gate. SETTLING feature flag: ON (default)
    # => `dev/push-gate.sh` scopes the diff (doctest + ast-grep), FULL on every
    # uncertainty; OFF => whole-tree both sweeps (the pre-#122 behaviour). The
    # default flipped to ON after the CI `selection-audit` soak showed the scoped
    # selection never missed a whole-tree finding. The flag survives as a per-overlay
    # escape hatch; the CI whole-tree backstop is never removed regardless.
    incremental_push_gate: bool = True
    # chrome-devtools-mcp is teatree's DEFAULT browser tool (navigation,
    # interaction, and network / console / DOM inspection over CDP — no claude.ai
    # account or extension pairing). When true, `t3 mcp browser-diagnosis` emits
    # the `claude mcp add` command that registers Google's chrome-devtools-mcp so
    # an agent can drive and inspect a deployed page before proposing a root cause
    # for browser-visible breakage. Default ON; perf/trace *enforcement* stays in
    # the deterministic Playwright lane, never this server. Per-overlay
    # overridable (DB-home) — turn OFF only on a host that cannot run the server.
    chrome_devtools_mcp_enabled: bool = True
    colleague_repo_url_pattern: str = ""
    solo_repo_url_pattern: str = ""
    # Conventional-Commits title pattern enforced at ``pr create`` BEFORE the
    # gh/glab network call (#1540). A non-matching title is rejected with the
    # pattern printed verbatim; the description is independently required to
    # carry a What/Why header. Per-overlay overridable via
    # ``[overlays.<name>].mr_title_regex = "…"`` so an overlay with a different
    # title grammar declares its own pattern without flipping the global.
    mr_title_regex: str = DEFAULT_MR_TITLE_REGEX


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
