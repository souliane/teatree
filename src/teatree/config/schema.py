"""The unified ``pydantic-settings`` model over teatree's 235 config keys.

``TeatreeSettingsSchema`` is the BASE-LAYER schema: it hosts the shipped default
VALUES (``defaults.toml``) behind teatree's existing per-key coercers and carries
the taxonomy (:class:`Category` / :class:`Registry`) as ``Annotated`` markers. It
does NOT reinvent teatree's #258 strict coercion — each field wraps the SAME
callable the four config registries already use (``setting_parsers`` /
``value_coercion`` / the enum ``.parse`` methods) as a ``BeforeValidator``, so the
model HOSTS teatree's strictness rather than substituting pydantic's own lax/strict
rules.

No runtime module imports this one: the four config registries stay
hand-maintained (``setting_registries`` / ``registries`` / ``cold_hook_settings``)
and the ``derive_*`` helpers below are consumed ONLY by the conformance suite
(``tests/config/test_registry_derivation.py``), which pins each hand registry equal
to what the model derives, key-for-key and coercer-for-coercer. That parity-pin is a
deliberate steady state, not a half-finished cutover — see the ``derive_*`` block for
why the runtime must never call these at import scope.

The ONE cost this module carries — importing ``pydantic_settings`` (~110ms) — is paid
only by a caller that imports ``schema``; the cold path (``cold_reader`` /
``cold_defaults`` / ``cold_hook_settings``, all reached through ``teatree.config``'s
package init) never imports ``schema``, so it never pays it. ``shipped_defaults()`` is
the ``@lru_cache`` singleton entry point.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, auto
from functools import lru_cache
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, TomlConfigSettingsSource

from teatree.config.agent_enums import AgentHarnessProvider, AgentRuntime, parse_harness_name
from teatree.config.cold_defaults import DEFAULTS_TOML as _DEFAULTS_TOML
from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.cold_hook_settings import ColdHookSetting
from teatree.config.enums import (
    Autonomy,
    CriticGateMode,
    MissingIssuePolicy,
    Mode,
    OnBehalfPostMode,
    PrReviewBackend,
    SendProxyMode,
    Wip,
)
from teatree.config.mr_reminder import parse_mr_reminder_setting
from teatree.config.registries import _parse_registry_dict
from teatree.config.setting_parsers import (
    _parse_handover_mirror_path,
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


class Category(StrEnum):
    """A key's shareability class — the axis ``defaults.toml`` and the export gate key on.

    ``DEFAULT`` keys carry a shareable shipped default and ARE present in
    ``defaults.toml`` (required in the model — a DEFAULT key missing from the file
    fails construction loudly). ``PERSONAL`` (operator identifiers / machine paths /
    model-routing tables) and ``SECRET`` (customer/brand terms, credential
    coordinates) keys hold no shareable default: they carry the empty code default
    and are NEVER written to ``defaults.toml``.
    """

    DEFAULT = auto()
    PERSONAL = auto()
    SECRET = auto()


class Registry(StrEnum):
    """Which of the four existing config-key registries a key belongs to."""

    OVERLAY = auto()
    COLD = auto()
    COLD_HOOK = auto()
    REGISTRY = auto()


@dataclass(frozen=True)
class SettingMeta:
    """The ``Annotated`` taxonomy marker carried on every field.

    Instances are shared per ``(category, registry)`` combo (the ``_<CAT>_<REG>``
    module constants below) so each field declaration stays one line under the
    120-col cap. Per-field prose would have to un-share them, so it belongs on a
    surface keyed by field name rather than on this marker.
    """

    category: Category
    registry: Registry


_DEFAULT_OVERLAY = SettingMeta(Category.DEFAULT, Registry.OVERLAY)
_DEFAULT_COLD = SettingMeta(Category.DEFAULT, Registry.COLD)
_DEFAULT_COLD_HOOK = SettingMeta(Category.DEFAULT, Registry.COLD_HOOK)
_PERSONAL_OVERLAY = SettingMeta(Category.PERSONAL, Registry.OVERLAY)
_PERSONAL_COLD = SettingMeta(Category.PERSONAL, Registry.COLD)
_PERSONAL_REGISTRY = SettingMeta(Category.PERSONAL, Registry.REGISTRY)
_SECRET_OVERLAY = SettingMeta(Category.SECRET, Registry.OVERLAY)
_SECRET_COLD = SettingMeta(Category.SECRET, Registry.COLD)


# Two closed value sets that have no enum of their own to point at. Declaring them
# here — rather than as bare ``str`` — is what makes ``setting_choices`` derive them,
# so the dashboard offers a select instead of a box an invalid value can be typed into.
# The empty member is a real state in both (auto-detect / unset), never a placeholder.
_RepoMode = Literal["", "solo", "collaborative"]
_Privacy = Literal["", "strict", "relaxed"]


def _provider_or_none(value: str | None) -> AgentHarnessProvider | None:
    # agent_harness_provider's None default means "inherit the ambient credential";
    # AgentHarnessProvider.parse rejects None, so the None sentinel passes through
    # while any real value is validated by the registry coercer.
    return None if value is None else AgentHarnessProvider.parse(value)


class _TeatreeTableTomlSource(TomlConfigSettingsSource):
    """Feed the model the ``[teatree]`` table of ``defaults.toml``, flattened to field names.

    The file renders the declaration hierarchy as nested group tables, so the table is
    flattened through the one Django-free reader
    (:func:`~teatree.config.cold_defaults.flatten_settings_table`) before it reaches the
    model — the model's fields are the flat namespace, and a group wrapper is not a field.
    The ``speak`` / ``mr_reminder`` sub-tables ARE fields, so the flattener leaves them
    whole and they arrive as the nested dicts those fields expect. No env source here:
    ``T3_*`` env handling stays in ``resolution.py``.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls, toml_file=_DEFAULTS_TOML)

    def __call__(self) -> dict[str, Any]:
        return flatten_settings_table(super().__call__().get("teatree", {}))


class TeatreeSettingsSchema(BaseSettings):
    """The 235-key config schema. See the module docstring for the design."""

    model_config = SettingsConfigDict(extra="forbid", validate_default=True, frozen=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # TOML-only base layer: env / dotenv / file-secret sources are dropped on purpose
        # (T3_* env handling stays in resolution.py); init wins so tests can pass overrides.
        _ = (env_settings, dotenv_settings, file_secret_settings)
        return (init_settings, _TeatreeTableTomlSource(settings_cls))

    admin_autologin_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    adaptive_intake_concurrency_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    admission_governor_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    admit_colleague_prs_to_board: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    agent_harness: Annotated[str, BeforeValidator(parse_harness_name), _DEFAULT_OVERLAY]
    agent_harness_provider: Annotated[
        AgentHarnessProvider | None, BeforeValidator(_provider_or_none), _PERSONAL_OVERLAY
    ] = None
    agent_runtime: Annotated[AgentRuntime, BeforeValidator(AgentRuntime.parse), _DEFAULT_OVERLAY]
    agent_signature: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    allow_destructive_disk: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    allow_destructive_ram: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    anthropic_api_key_pass_paths: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_OVERLAY] = []
    anthropic_oauth_pass_paths: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_OVERLAY] = []
    approved_recipe_sha: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    architectural_review_after_merge_count: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    architectural_review_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    architectural_review_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    architectural_review_retry_backoff_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    architectural_review_skill: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    ask_before_backlog_sweep_closes: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    ask_before_creating_news_tickets: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    attachment_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    auto_disposition_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    auto_disposition_max_closes_per_tick: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    auto_update_reinstall: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    auto_update_require_green_main: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    autoload: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    autonomy: Annotated[Autonomy, BeforeValidator(Autonomy.parse), _DEFAULT_OVERLAY]
    backlog_sweep_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    backlog_sweep_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    backlog_sweep_skill: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    ban_close_trailers_on_namespaces: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    billing_cycle_anchor_day: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    boost_concurrency: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    bulk_close_threshold: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    cheap_phase_admission_ceiling: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    check_updates: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    chrome_devtools_headless: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    chrome_devtools_mcp_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    ci_eval_heal_autofix_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    claude_chrome: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    clean_ignore: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    colleague_repo_url_pattern: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    contribute: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    contribute_plugin_dir: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    critic_gate_mode: Annotated[CriticGateMode, BeforeValidator(CriticGateMode.parse), _DEFAULT_OVERLAY]
    dashboard_instance_label: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    db_backup_cadence_hours: Annotated[int, BeforeValidator(_parse_overridable_positive_int(24)), _DEFAULT_OVERLAY]
    db_backup_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    db_backup_retention_days: Annotated[int, BeforeValidator(_parse_overridable_positive_int(7)), _DEFAULT_OVERLAY]
    directive_intake_per_tick: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    directive_loop_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    directive_verify_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    disk_cache_allowlist: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    disk_crit_free_gb: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    disk_warn_free_gb: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    dogfood_smoke_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    dogfood_smoke_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    dogfood_smoke_overlay: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    dogfood_smoke_skill: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    dream_propose_evals: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    e2e_confidence_threshold: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    e2e_mandatory_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    enforce_regulated_path: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    envelope_stop_gate_refusals: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    eval_local_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    eval_local_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    eval_local_skill: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    excluded_skills: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    expected_required_contexts: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    factory_score_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    fleet_claim_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    gate_relaxation_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    gitlab_approval_scanner_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    handover_mirror_path: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_OVERLAY] = ""
    headless_max_turns: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    hook_fetch_titles: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    idle_stack_e2e_recent_minutes: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    idle_stack_idle_minutes: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    idle_stack_reaper_cadence_minutes: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    idle_stack_reaper_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    incoming_event_retention_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    incremental_push_gate: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    intake_ram_per_agent_gb: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    intake_ram_reserve_gb: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    issue_implementer_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    issue_implementer_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    issue_implementer_label: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    issue_implementer_max_concurrent: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    limit_autorecovery_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    local_stack_queue_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    local_stack_queue_max_attempts: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    loop_cadence_seconds: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    loop_runner_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    max_concurrent_local_stacks: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    max_open_prs_per_repo_per_ticket: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    max_worktree_gc_per_tick: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    merge_wip: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    missing_issue_ref_policy: Annotated[MissingIssuePolicy, BeforeValidator(MissingIssuePolicy.parse), _DEFAULT_OVERLAY]
    mode: Annotated[Mode, BeforeValidator(Mode.parse), _DEFAULT_OVERLAY]
    mr_conflict_scan_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    mr_reminder: Annotated[dict[str, Any], BeforeValidator(parse_mr_reminder_setting), _DEFAULT_OVERLAY]
    mr_state_questions_max_per_tick: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    mr_title_regex: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    notify_on_post_on_behalf: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    notify_user_via_bot: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    on_behalf_auto_actions: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    on_behalf_post_mode: Annotated[OnBehalfPostMode, BeforeValidator(OnBehalfPostMode.parse), _DEFAULT_OVERLAY]
    openai_compatible_base_url: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_OVERLAY] = ""
    openai_compatible_credential_entry: Annotated[str, BeforeValidator(_parse_strict_str), _SECRET_OVERLAY] = ""
    openai_compatible_lane: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    openai_compatible_model: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_OVERLAY] = ""
    orchestrate_claim_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    orchestrator_bash_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    outer_loop_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    outer_loop_max_per_week: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    outer_loop_measure_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    outer_loop_stop_after_consecutive_failures: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    park_attempt_retention_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    privacy: Annotated[_Privacy, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    pr_review_backend: Annotated[PrReviewBackend, BeforeValidator(PrReviewBackend.parse), _DEFAULT_OVERLAY]
    provision_fast_step_timeout_seconds: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    provision_max_concurrency: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    provision_ram_ceiling_percent: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    provision_slow_threshold_seconds: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    provision_step_timeout_seconds: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    pull_main_clone_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    pull_main_clone_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    pydantic_ai_max_tokens: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    pydantic_ai_request_limit: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    ram_crit_avail_gb: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    ram_kill_allowlist: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    ram_warn_avail_gb: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    regulated_path_model_allowlist: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    repo_mode: Annotated[_RepoMode, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    require_anti_vacuity_attestation: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_debt_delta: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_executed_repro: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_human_approval_to_answer: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_human_approval_to_merge: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_integration_review: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_merge_evidence: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_merge_quality_verdict: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_plan_adequacy: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_review_context: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_reviewed_state_for_review_request: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_rubric_verification: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_spec_coverage: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    require_work_group_batch: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    resource_pressure_cadence_minutes: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    resource_pressure_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    resource_pressure_min_free_interval_minutes: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    review_backend_cooldown_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    review_exempt_repos: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    review_exempt_repos_count_toward_group_readiness: Annotated[
        bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY
    ]
    review_nag_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    review_nag_max_interval_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    review_pause_reaction_emojis: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    review_request_dedup_max_pages: Annotated[
        int, BeforeValidator(_parse_overridable_positive_int(5)), _DEFAULT_OVERLAY
    ]
    review_request_dedup_window_days: Annotated[
        int, BeforeValidator(_parse_overridable_positive_int(30)), _DEFAULT_OVERLAY
    ]
    review_request_post_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    review_resume_reply_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    review_skill: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    review_skill_alternates: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    scanning_news_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    scanning_news_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    scanning_news_skill: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    sdk_monthly_credit_usd: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    schema_readiness_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    self_update_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    self_update_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    send_proxy_allowlist: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    send_proxy_mode: Annotated[SendProxyMode, BeforeValidator(SendProxyMode.parse), _DEFAULT_OVERLAY]
    session_stale_after_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    slack_voice_classifier_mode: Annotated[
        SlackVoiceClassifierMode, BeforeValidator(SlackVoiceClassifierMode.parse), _DEFAULT_OVERLAY
    ]
    snapshot_baseline_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    snapshot_warmer_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    snapshot_warmer_max_age_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    single_branch_repos: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    solo_repo_url_pattern: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    speak: Annotated[dict[str, Any], BeforeValidator(parse_speak_setting), _DEFAULT_OVERLAY]
    stale_stack_min_age_minutes: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    statusline_chain: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    statusline_engaged_render: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    substrate_auto_merge_authorized_by: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    substrate_self_signoff: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    subagent_spawn_ceiling: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    task_attempt_retention_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    task_result_retention_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    task_sweep_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    task_sweep_recheck_interval_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    target_branch: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    ticket_budget_max_cost_usd: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    ticket_transition_prune_disabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    timezone: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_OVERLAY]
    mr_triage_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    mr_triage_max_mrs_per_tick: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    triage_assessor_cadence_hours: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    triage_assessor_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    triage_assessor_max_issues_per_tick: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    trusted_issue_authors: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    user_identity_aliases: Annotated[list[str], BeforeValidator(_parse_user_identity_aliases), _PERSONAL_OVERLAY] = []
    watchdog_max_cost_usd: Annotated[float, BeforeValidator(_parse_strict_float), _DEFAULT_OVERLAY]
    watchdog_max_runtime_seconds: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    watchdog_max_turns: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    wip: Annotated[Wip, BeforeValidator(Wip.parse), _DEFAULT_OVERLAY]
    work_group_generic_scopes: Annotated[list[str], BeforeValidator(_parse_str_list), _DEFAULT_OVERLAY]
    work_group_max_members: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    worker_quiescing: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_OVERLAY]
    workspace_dir: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_OVERLAY] = ""
    worktree_stale_days: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]
    write_wip: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_OVERLAY]

    # --- COLD_SETTINGS (cold-read DB tier) ---
    active_loop_schedule: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_COLD]
    agent_honesty_model: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_COLD] = ""
    agent_phase_fanout: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    agent_phase_harness: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    agent_phase_models: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    agent_pydantic_ai_tier_models: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    agent_session_effort: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_COLD] = ""
    agent_session_model: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_COLD] = ""
    agent_skill_models: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    agent_tier_effort: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    agent_tier_models: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    availability_schedule: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    banned_brands: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_COLD] = []
    banned_term_registry: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _SECRET_COLD] = {}
    banned_terms: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_COLD] = []
    banned_terms_allowlist: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_COLD] = []
    cost_model_prices: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    danger_gate_fail_open: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD]
    internal_publish_namespaces: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_COLD] = []
    loops: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}
    low_power_auto_engage: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD]
    low_power_preset_name: Annotated[str, BeforeValidator(_parse_strict_str), _DEFAULT_COLD]
    overlay_leak_terms: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_COLD] = []
    private_repos: Annotated[list[str], BeforeValidator(_parse_str_list), _SECRET_COLD] = []
    private_tests: Annotated[str, BeforeValidator(_parse_strict_str), _SECRET_COLD] = ""
    slack_user_channel: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_COLD] = ""
    slack_user_id: Annotated[str, BeforeValidator(_parse_strict_str), _PERSONAL_COLD] = ""
    timeouts: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_COLD] = {}

    # --- COLD_HOOK_SETTINGS (pre-Django hook gate flags) ---
    banned_terms_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    banned_terms_required: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    completion_claim_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    config_overwrite_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    deny_circuit_breaker_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    deny_circuit_breaker_threshold: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_COLD_HOOK]
    dispatch_quote_gate_on_task_create_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    glab_stale_base_remote_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    git_add_all_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    hook_validator_timeout_seconds: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_COLD_HOOK]
    headless_authoring_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    main_clone_guard_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    single_branch_repo_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    mcp_privacy_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    mcp_slack_write_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    memory_recall_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    no_self_reviewer_assign_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    orchestrator_boundary_agent_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    orchestrator_investigation_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    orchestrator_turn_budget: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_COLD_HOOK]
    orchestrator_turn_wall_clock_seconds: Annotated[int, BeforeValidator(_parse_strict_int), _DEFAULT_COLD_HOOK]
    out_of_band_merge_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    plan_edit_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    self_dm_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    skill_loading_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    standing_goal_stop_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    stop_snapshotter_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]
    unknown_repo_push_gate_enabled: Annotated[bool, BeforeValidator(_parse_strict_bool), _DEFAULT_COLD_HOOK]

    # --- REGISTRY_SETTINGS (overlays + e2e_repos definition registries) ---
    e2e_repos: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_REGISTRY] = {}
    overlays: Annotated[dict[str, Any], BeforeValidator(_parse_registry_dict), _PERSONAL_REGISTRY] = {}


def setting_meta(key: str) -> SettingMeta:
    """The :class:`SettingMeta` marker declared on field *key* (every field carries one)."""
    return next(m for m in TeatreeSettingsSchema.model_fields[key].metadata if isinstance(m, SettingMeta))


def _enumerated(annotation: object) -> tuple[object, ...]:
    """The values *annotation* admits, or ``()`` when it admits more than a listable set.

    ``bool`` and every ``StrEnum`` the config declares are closed sets, and a ``Literal`` is
    one by construction. An optional wrapper (``X | None``) contributes its ``None`` beside
    the members of ``X``, so a nullable enum still offers every value it accepts.
    """
    if annotation is bool:
        return (True, False)
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    if get_origin(annotation) in {Union, UnionType}:
        return tuple(v for arg in get_args(annotation) for v in ((None,) if arg is NoneType else _enumerated(arg)))
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return tuple(member.value for member in annotation)
    return ()


def setting_choices(key: str) -> tuple[object, ...]:
    """*key*'s admissible values when the schema constrains it to a listable set.

    The ONE derivation behind every constrained control teatree renders. An option list
    written out beside a ``Literal`` is a second source that goes stale the first time the
    ``Literal`` changes; deriving it here also makes an invalid value impossible to ENTER
    rather than merely rejected after the fact. A key whose type admits open values
    (``str``, ``int``, a list, a mapping) returns ``()`` — the caller renders free text.

    The values come back RAW, in the schema's own vocabulary; how a surface writes one on
    the wire and how it labels one on screen are that surface's decisions, not this one's.
    """
    field = TeatreeSettingsSchema.model_fields.get(key)
    return () if field is None else _enumerated(field.annotation)


_NO_INIT_OVERRIDES: dict[str, Any] = {}


@lru_cache(maxsize=1)
def shipped_defaults() -> TeatreeSettingsSchema:
    """The shipped-default singleton, constructed from ``defaults.toml``.

    ``@lru_cache`` so a malformed ``defaults.toml`` fails once, loudly, at first
    call rather than shipping a half-built settings object. The required fields are
    filled by the TOML source, not the constructor call — spread through an empty
    dict so the type checker doesn't read the source-filled fields as unpassed.
    """
    return TeatreeSettingsSchema(**_NO_INIT_OVERRIDES)


# The two keys whose RESOLVE-tier registry coercer differs from the model's
# STORAGE-tier ``BeforeValidator`` (the same pair the Phase-1 parity matrix
# documents via its ``_STORAGE_COERCER`` override). The resolver needs a real
# ``Path`` / a None-rejecting parse, while the model stores a ``str`` / tolerates
# the ``None`` default — so the derivation re-sources the registry coercer for
# these two rather than reusing the field validator.
_RESOLVE_TIER_COERCER: dict[str, Callable[[Any], Any]] = {
    "handover_mirror_path": _parse_handover_mirror_path,
    "agent_harness_provider": AgentHarnessProvider.parse,
}


def _before_validator(key: str) -> Callable[[Any], Any]:
    return next(m.func for m in TeatreeSettingsSchema.model_fields[key].metadata if isinstance(m, BeforeValidator))


def _registry_coercer(key: str) -> Callable[[Any], Any]:
    """The resolve-tier coercer a derived registry binds for *key*.

    The storage-tier ``BeforeValidator`` for every key but the two documented
    resolve-tier ones above, which re-source their registry coercer.
    """
    return _RESOLVE_TIER_COERCER.get(key) or _before_validator(key)


def _keys_in(registry: Registry) -> list[str]:
    return [k for k in TeatreeSettingsSchema.model_fields if setting_meta(k).registry is registry]


# COLD-PATH PARITY PIN — deliberate; do NOT wire these into the registry modules.
# The four ``derive_*`` helpers reconstruct each hand registry from the model taxonomy,
# and the conformance suite asserts they match the hand copies exactly. They are
# call-time helpers ONLY: none may be assigned at the import scope of
# ``setting_registries`` / ``registries`` / ``cold_hook_settings``. ``teatree.config``'s
# package ``__init__`` eagerly imports all three of those modules, and the cold path
# loads that package init (``cold_reader`` does ``from teatree.config import
# value_coercion``), so a module-scope ``derive_*()`` call would drag ``schema`` ->
# pydantic (~110ms) onto every cold-hook invocation. The registries therefore stay
# hand-maintained; these helpers exist to keep the hand copies honest via the parity
# suite, not to replace them at runtime. The leak guard is
# ``test_registry_derivation.test_registry_modules_stay_pydantic_free`` (with
# ``test_cold_defaults``'s cold-import control).
def derive_overlay_overridable_settings() -> dict[str, Callable[[Any], Any]]:
    """The ``OVERLAY_OVERRIDABLE_SETTINGS`` registry, derived from the model taxonomy."""
    return {k: _registry_coercer(k) for k in _keys_in(Registry.OVERLAY)}


def derive_cold_settings() -> dict[str, Callable[[Any], Any]]:
    """The ``COLD_SETTINGS`` registry, derived from the model taxonomy."""
    return {k: _registry_coercer(k) for k in _keys_in(Registry.COLD)}


def derive_registry_settings() -> dict[str, Callable[[Any], Any]]:
    """The ``REGISTRY_SETTINGS`` registry, derived from the model taxonomy."""
    return {k: _registry_coercer(k) for k in _keys_in(Registry.REGISTRY)}


def derive_cold_hook_settings() -> dict[str, ColdHookSetting]:
    """The ``COLD_HOOK_SETTINGS`` registry, derived from the model taxonomy + defaults.

    Each cold-hook key is Default-category (present in ``defaults.toml``), so its
    in-code fallback is the model's shipped default; the scope is GLOBAL (``""``) —
    cold-hook settings are never per-overlay overridable.
    """
    defaults = shipped_defaults()
    return {
        k: ColdHookSetting(_registry_coercer(k), default=getattr(defaults, k)) for k in _keys_in(Registry.COLD_HOOK)
    }
