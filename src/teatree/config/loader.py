"""TeaTree config loading — ``load_config`` + the toml/logging/dir entry points.

``CONFIG_PATH``, ``load_config`` (builds ``UserSettings`` from ``~/.teatree.toml``),
the toml loader, the default Django LOGGING dict, ``load_e2e_repos``, and the
``workspace_dir`` / ``worktrees_dir`` / ``check_for_updates`` resolvers. Split out
of the package facade for the RUF067 init-is-re-exports-only rule; re-exported
from ``teatree.config`` so every ``teatree.config.<name>`` path stays valid. The
per-setting resolvers live in ``resolution`` and are reached through the package
facade at call-time (the partition's loader -> resolution edge, deferred to avoid
the loader/resolution/discovery import cycle).

``load_config`` builds only the **file tier** (the global ``[teatree]`` table
merged onto the dataclass defaults). The higher tiers — env, the #1775 DB
override tier (``ConfigSetting`` rows), and the per-overlay ``[overlays.<name>]``
table — are layered on top by ``resolution.get_effective_settings``; consult its
docstring for the full ``env -> DB -> per-overlay -> global -> default``
precedence. Callers that need effective values must use ``get_effective_settings``,
not the bare ``load_config().user`` (which sees neither env, DB, nor per-overlay).
"""

import tomllib
from pathlib import Path

import teatree.config as _facade
from teatree.config.enums import Autonomy, MissingIssuePolicy, Mode, Speed, TeamsDisplay
from teatree.config.resolution import (
    _resolve_enum_setting,
    _resolve_on_behalf_post_mode,
    _resolve_slack_voice_classifier_mode,
)
from teatree.config.settings import (
    E2ERepo,
    TeaTreeConfig,
    UserSettings,
    _default_handover_mirror_path,
    _parse_disk_cache_allowlist,
    _parse_on_behalf_auto_actions,
)
from teatree.config_mr_reminder import resolve_mr_reminder
from teatree.config_speak import resolve_speak
from teatree.paths import DATA_DIR, get_data_dir
from teatree.types import DEFAULT_MR_TITLE_REGEX
from teatree.update_check import run_update_check

CONFIG_PATH = Path.home() / ".teatree.toml"


def default_logging(namespace: str) -> dict:
    """Return a default Django LOGGING dict that writes to ``<data_dir>/logs/teatree.log``.

    Usage in settings::

        from teatree.config import default_logging
        LOGGING = default_logging("my_overlay")
    """
    log_dir = get_data_dir(namespace) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "verbose": {
                "format": "{asctime} {levelname} {name} {message}",
                "style": "{",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "teatree.log"),
                "maxBytes": 5_000_000,
                "backupCount": 3,
                "formatter": "verbose",
            },
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "verbose",
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": "INFO",
        },
        "loggers": {
            "django.request": {"level": "INFO", "propagate": True},
            "teatree": {"level": "DEBUG", "propagate": True},
        },
    }


def _load_toml(path: Path) -> dict:
    """Parse ``path`` as TOML, re-raising a syntax error as a named config error.

    A raw ``tomllib.TOMLDecodeError`` would propagate a parser traceback
    through ``main()`` on every ``t3`` command (even ``--help``); instead it
    becomes a typed, message-bearing ``ValueError`` naming the file and the
    parser's position — the same error shape the intentional invalid-``mode``
    path raises.
    """
    with path.open("rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            msg = f"Malformed TOML in config file {path}: {exc}"
            raise ValueError(msg) from exc


def _file_str_list(raw: object) -> list[str]:
    """Lenient list coercion for the FILE tier (``load_config``).

    A real TOML/JSON array coerces each element to ``str``; a malformed scalar
    degrades to an empty list rather than raising — the global ``~/.teatree.toml``
    has always been tolerant of a stray scalar here (the file tier never hard-
    fails the whole config on one malformed key). The OVERRIDE tier (per-overlay
    / DB) is the strict one: it routes through ``settings._parse_str_list`` which
    RAISES on a non-list scalar (#258), so a bad override is rejected loud while
    a bad global file still loads.
    """
    return [str(s) for s in raw] if isinstance(raw, list) else []


def _resolve_teams_enabled(raw: dict) -> bool:
    """Resolve the global ``teams_enabled`` value from the top-level ``[teams]`` table.

    The inert agent-teams WORK layer reads its enable flag from
    ``[teams] enabled`` — the natural namespace for the feature, mirroring how
    ``[mr_reminder]`` / ``[teatree.speak]`` read a dedicated table into a
    ``UserSettings`` field. An absent ``[teams]`` table or an absent ``enabled``
    key resolves to ``False`` (ships dark). The per-overlay / env tiers key on
    the ``teams_enabled`` field name and are layered by
    ``get_effective_settings``.
    """
    table = raw.get("teams")
    if isinstance(table, dict):
        return bool(table.get("enabled", False))
    return False


def _resolve_teams_int(raw: dict, key: str, default: int) -> int:
    """Resolve a positive-int value from the top-level ``[teams]`` table, fail-safe.

    The pane-budget settings (``[teams] max_panes`` / ``[teams] idle_minutes``,
    #1838 PR#7a) live in the ``[teams]`` namespace alongside ``enabled``. An
    absent table/key, a non-int, a ``bool``, or a non-positive value all degrade
    to *default* — the safety bound the setting encodes can never be disabled by
    a mistyped config value. The per-overlay / env tiers key on the matching
    ``teams_*`` field name and are layered by ``get_effective_settings``.
    """
    table = raw.get("teams")
    if not isinstance(table, dict):
        return default
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value > 0 else default


def _resolve_teams_display(raw: dict) -> TeamsDisplay:
    """Resolve the global ``teams_display`` value from ``[teams] display`` (#1838 WI-5).

    The PRESENTATION-only pane-display mode lives in the ``[teams]`` namespace
    alongside ``enabled`` / ``max_panes`` / ``idle_minutes``. An absent ``[teams]``
    table or an absent ``display`` key resolves to the conservative
    :attr:`TeamsDisplay.NONE` (ships dark). An explicit but invalid value raises
    via :meth:`TeamsDisplay.parse` — a typo in the global file is loud, never a
    silent escalation. The per-overlay / env tiers key on the ``teams_display``
    field name and are layered by ``get_effective_settings``.
    """
    table = raw.get("teams")
    if not isinstance(table, dict):
        return TeamsDisplay.NONE
    value = table.get("display")
    return TeamsDisplay.parse(value) if value is not None else TeamsDisplay.NONE


def load_config(path: Path | None = None) -> TeaTreeConfig:
    if path is None:
        path = _facade.CONFIG_PATH
    if not path.is_file():
        return TeaTreeConfig()

    raw = _load_toml(path)

    teatree = raw.get("teatree", {})
    workspace_dir = Path(teatree.get("workspace_dir", "~/workspace")).expanduser()
    worktrees_dir = Path(teatree.get("worktrees_dir", str(DATA_DIR / "worktrees"))).expanduser()

    raw_excluded = teatree.get("excluded_skills", [])
    excluded_skills = [str(s) for s in raw_excluded] if isinstance(raw_excluded, list) else []

    toml_mode = teatree.get("mode")
    mode = Mode.parse(toml_mode) if toml_mode is not None else Mode.INTERACTIVE

    on_behalf_post_mode, ask_before_post_on_behalf = _resolve_on_behalf_post_mode(teatree)

    publish_gates = teatree.get("publish_gates", {}) if isinstance(teatree, dict) else {}
    raw_ban = publish_gates.get("ban_close_trailers_on_namespaces", []) if isinstance(publish_gates, dict) else []
    ban_close_trailers_on_namespaces = (
        [str(p) for p in raw_ban if isinstance(p, str) and p] if isinstance(raw_ban, list) else []
    )

    user = UserSettings(
        workspace_dir=workspace_dir,
        worktrees_dir=worktrees_dir,
        branch_prefix=teatree.get("branch_prefix", ""),
        privacy=teatree.get("privacy", ""),
        check_updates=teatree.get("check_updates", True),
        timezone=teatree.get("timezone", ""),
        contribute=bool(teatree.get("contribute", False)),
        excluded_skills=excluded_skills,
        redis_db_count=int(teatree.get("redis_db_count", 16)),
        mode=mode,
        autonomy=_resolve_enum_setting(teatree, "autonomy", Autonomy, Autonomy.BABYSIT),
        speed=_resolve_enum_setting(teatree, "speed", Speed, Speed.MEDIUM),
        loop_cadence_seconds=int(teatree.get("loop_cadence_seconds", 720)),
        dedicated_loops=bool(teatree.get("dedicated_loops", False)),
        teams_enabled=_resolve_teams_enabled(raw),
        teams_max_panes=_resolve_teams_int(raw, "max_panes", 1),
        teams_idle_minutes=_resolve_teams_int(raw, "idle_minutes", 30),
        teams_display=_resolve_teams_display(raw),
        require_human_approval_to_merge=bool(teatree.get("require_human_approval_to_merge", True)),
        require_human_approval_to_answer=bool(teatree.get("require_human_approval_to_answer", True)),
        ask_before_post_on_behalf=ask_before_post_on_behalf,
        on_behalf_post_mode=on_behalf_post_mode,
        on_behalf_auto_actions=_parse_on_behalf_auto_actions(teatree.get("on_behalf_auto_actions")),
        notify_user_via_bot=bool(teatree.get("notify_user_via_bot", True)),
        notify_on_post_on_behalf=bool(teatree.get("notify_on_post_on_behalf", True)),
        claude_chrome=bool(teatree.get("claude_chrome", True)),
        agent_signature=bool(teatree.get("agent_signature", False)),
        statusline_chain=[str(s) for s in teatree.get("statusline_chain", [])],
        user_identity_aliases=_file_str_list(teatree.get("user_identity_aliases", [])),
        repo_mode=str(teatree.get("repo_mode", "")),
        missing_issue_ref_policy=_resolve_enum_setting(
            teatree,
            "missing_issue_ref_policy",
            MissingIssuePolicy,
            MissingIssuePolicy.FIND_EXISTING_THEN_ASK,
        ),
        architectural_review_disabled=bool(teatree.get("architectural_review_disabled", False)),
        architectural_review_skill=str(teatree.get("architectural_review_skill", "ac-reviewing-codebase")),
        architectural_review_cadence_hours=int(teatree.get("architectural_review_cadence_hours", 168)),
        architectural_review_after_merge_count=int(teatree.get("architectural_review_after_merge_count", 25)),
        review_skill=str(teatree.get("review_skill", "")),
        require_review_context=bool(teatree.get("require_review_context", False)),
        require_anti_vacuity_attestation=bool(teatree.get("require_anti_vacuity_attestation", False)),
        require_rubric_verification=bool(teatree.get("require_rubric_verification", False)),
        require_spec_coverage=bool(teatree.get("require_spec_coverage", False)),
        scanning_news_disabled=bool(teatree.get("scanning_news_disabled", False)),
        scanning_news_skill=str(teatree.get("scanning_news_skill", "scanning-news")),
        scanning_news_cadence_hours=int(teatree.get("scanning_news_cadence_hours", 24)),
        ask_before_creating_news_tickets=bool(teatree.get("ask_before_creating_news_tickets", True)),
        eval_local_disabled=bool(teatree.get("eval_local_disabled", False)),
        eval_local_skill=str(teatree.get("eval_local_skill", "eval")),
        eval_local_cadence_hours=int(teatree.get("eval_local_cadence_hours", 168)),
        dogfood_smoke_disabled=bool(teatree.get("dogfood_smoke_disabled", False)),
        dogfood_smoke_skill=str(teatree.get("dogfood_smoke_skill", "dogfood-smoke")),
        dogfood_smoke_cadence_hours=int(teatree.get("dogfood_smoke_cadence_hours", 24)),
        dogfood_smoke_overlay=str(teatree.get("dogfood_smoke_overlay", "")),
        self_update_disabled=bool(teatree.get("self_update_disabled", False)),
        self_update_cadence_hours=int(teatree.get("self_update_cadence_hours", 1)),
        auto_update_reinstall=bool(teatree.get("auto_update_reinstall", False)),
        auto_update_require_green_main=bool(teatree.get("auto_update_require_green_main", True)),
        resource_pressure_disabled=bool(teatree.get("resource_pressure_disabled", False)),
        resource_pressure_cadence_minutes=int(teatree.get("resource_pressure_cadence_minutes", 5)),
        resource_pressure_min_free_interval_minutes=int(
            teatree.get("resource_pressure_min_free_interval_minutes", 30),
        ),
        disk_warn_free_gb=float(teatree.get("disk_warn_free_gb", 25.0)),
        disk_crit_free_gb=float(teatree.get("disk_crit_free_gb", 10.0)),
        ram_warn_avail_gb=float(teatree.get("ram_warn_avail_gb", 3.0)),
        ram_crit_avail_gb=float(teatree.get("ram_crit_avail_gb", 1.5)),
        disk_cache_allowlist=_parse_disk_cache_allowlist(teatree.get("disk_cache_allowlist")),
        allow_destructive_disk=bool(teatree.get("allow_destructive_disk", False)),
        worktree_stale_days=int(teatree.get("worktree_stale_days", 30)),
        max_worktree_gc_per_tick=int(teatree.get("max_worktree_gc_per_tick", 3)),
        allow_destructive_ram=bool(teatree.get("allow_destructive_ram", False)),
        ram_kill_allowlist=_file_str_list(teatree.get("ram_kill_allowlist", [])),
        todo_sweep_disabled=bool(teatree.get("todo_sweep_disabled", False)),
        todo_sweep_recheck_interval_hours=int(teatree.get("todo_sweep_recheck_interval_hours", 1)),
        max_concurrent_local_stacks=int(teatree.get("max_concurrent_local_stacks", 0)),
        provision_step_timeout_seconds=int(teatree.get("provision_step_timeout_seconds", 1800)),
        idle_stack_reaper_disabled=bool(teatree.get("idle_stack_reaper_disabled", False)),
        idle_stack_idle_minutes=int(teatree.get("idle_stack_idle_minutes", 30)),
        idle_stack_reaper_cadence_minutes=int(teatree.get("idle_stack_reaper_cadence_minutes", 5)),
        idle_stack_e2e_recent_minutes=int(teatree.get("idle_stack_e2e_recent_minutes", 60)),
        stale_stack_min_age_minutes=int(teatree.get("stale_stack_min_age_minutes", 0)),
        local_stack_queue_disabled=bool(teatree.get("local_stack_queue_disabled", False)),
        local_stack_queue_max_attempts=int(teatree.get("local_stack_queue_max_attempts", 13)),
        clean_ignore=_file_str_list(teatree.get("clean_ignore", [])),
        slack_voice_classifier_mode=_resolve_slack_voice_classifier_mode(teatree),
        speak=resolve_speak(teatree),
        mr_reminder=resolve_mr_reminder(raw),
        ban_close_trailers_on_namespaces=ban_close_trailers_on_namespaces,
        pull_main_clone_disabled=bool(teatree.get("pull_main_clone_disabled", False)),
        pull_main_clone_cadence_hours=int(teatree.get("pull_main_clone_cadence_hours", 1)),
        review_nag_enabled=bool(teatree.get("review_nag_enabled", False)),
        orchestrator_bash_gate_enabled=bool(teatree.get("orchestrator_bash_gate_enabled", True)),
        e2e_mandatory_gate_enabled=bool(teatree.get("e2e_mandatory_gate_enabled", True)),
        mr_title_regex=str(teatree.get("mr_title_regex", DEFAULT_MR_TITLE_REGEX)),
        issue_implementer_enabled=bool(teatree.get("issue_implementer_enabled", False)),
        issue_implementer_label=str(teatree.get("issue_implementer_label", "")),
        issue_implementer_max_concurrent=int(teatree.get("issue_implementer_max_concurrent", 1)),
        issue_implementer_cadence_hours=int(teatree.get("issue_implementer_cadence_hours", 1)),
        auto_disposition_enabled=bool(teatree.get("auto_disposition_enabled", False)),
        auto_disposition_max_closes_per_tick=int(teatree.get("auto_disposition_max_closes_per_tick", 5)),
        orchestrate_claim_enabled=bool(teatree.get("orchestrate_claim_enabled", False)),
        handover_mirror_path=(
            Path(str(teatree["handover_mirror_path"])).expanduser()
            if teatree.get("handover_mirror_path")
            else _default_handover_mirror_path()
        ),
        billing_cycle_anchor_day=int(teatree.get("billing_cycle_anchor_day", 0)),
        sdk_monthly_credit_usd=float(teatree.get("sdk_monthly_credit_usd", 200.0)),
    )

    return TeaTreeConfig(user=user, raw=raw)


def load_e2e_repos(path: Path | None = None) -> list[E2ERepo]:
    """Load named E2E repos from ``[e2e_repos.<name>]`` sections in ``~/.teatree.toml``.

    Each entry may specify ``url``, ``branch``, and optionally ``e2e_dir``
    (the subdirectory containing ``playwright.config.ts``, default ``"e2e"``).
    """
    config = _facade.load_config(path)
    repos = []
    for name, entry in config.raw.get("e2e_repos", {}).items():
        repos.append(
            E2ERepo(
                name=name,
                url=entry.get("url", ""),
                branch=entry.get("branch", "main"),
                e2e_dir=entry.get("e2e_dir", "e2e"),
            )
        )
    return repos


def workspace_dir() -> Path:
    """Canonical workspace directory (where main repo clones live)."""
    from django.conf import settings  # noqa: PLC0415

    if hasattr(settings, "T3_WORKSPACE_DIR"):
        return Path(settings.T3_WORKSPACE_DIR)
    return _facade.load_config().user.workspace_dir


def worktrees_dir() -> Path:
    """Canonical worktrees directory (where ticket worktrees are created)."""
    from django.conf import settings  # noqa: PLC0415

    if hasattr(settings, "T3_WORKTREES_DIR"):
        return Path(settings.T3_WORKTREES_DIR)
    return _facade.load_config().user.worktrees_dir


def check_for_updates(*, force: bool = False) -> str | None:
    """Resolve a "new release available" notice from config + update_check.

    Reads ``check_updates`` from user config and delegates to
    :func:`teatree.update_check.run_update_check`. The implementation
    lives in :mod:`teatree.update_check` (split out for module-health
    LOC); this wrapper is the config-aware entry point.
    """
    return run_update_check(check_updates=_facade.load_config().user.check_updates, force=force)
