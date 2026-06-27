"""TeaTree config dataclasses + the per-overlay / env override registries.

``UserSettings`` (the ``[teatree]`` table), ``TeaTreeConfig``, ``OverlayEntry``,
``E2ERepo``, the field ``_parse_*`` coercers, and the two override registries
(``OVERLAY_OVERRIDABLE_SETTINGS`` / ``ENV_SETTING_OVERRIDES``). Split out of the
package module for the module-health LOC cap; re-exported from
``teatree.config`` so every ``teatree.config.<name>`` path stays valid.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from teatree.config.enums import AgentRuntime, Autonomy, MissingIssuePolicy, Mode, OnBehalfPostMode, Speed, TeamsDisplay
from teatree.config_mr_reminder import MrReminderConfig
from teatree.paths import DATA_DIR
from teatree.types import DEFAULT_MR_TITLE_REGEX, SlackVoiceClassifierMode, SpeakConfig


@dataclass
class E2ERepo:
    """An external git repository containing Playwright E2E tests."""

    name: str
    url: str
    branch: str
    e2e_dir: str = "e2e"


def _parse_str_list(raw: object) -> list[str]:
    """Coerce a list-typed overridable setting to ``list[str]``, strictly.

    A real list (TOML/JSON array) coerces each element to ``str``; ANY non-list
    scalar — a bool, an int, a bare string — RAISES ``TypeError`` rather than
    silently degrading to ``[]`` (#258). The old defaulting behaviour
    (``return [] if not a list``) let ``config_setting set excluded_skills true``
    pass write-time validation and persist the raw ``True``: a corrupt override
    masked as an empty list with no signal. The strict parser is the single
    coercer for every list-typed overridable setting, so the write path
    (validation) and the read path (DB-tier coercion) reject a scalar
    identically.
    """
    if not isinstance(raw, list):
        msg = f"Invalid list value {raw!r}; expected a JSON/TOML array, not a scalar"
        raise TypeError(msg)
    return [str(s) for s in raw]


_DEFAULT_DISK_CACHE_ALLOWLIST = ("~/.cache/pre-commit", "~/.cache/puppeteer", "~/.cache/codex-runtimes")


def _parse_disk_cache_allowlist(raw: object) -> list[str]:
    """Coerce the disk cache allow-list, falling back to the regenerable-cache default.

    A missing key (``None``) yields the curated default set of regenerable
    caches; an explicit list (even empty) is honoured verbatim so a user can
    narrow the allow-list to nothing. Non-list scalars degrade to the default
    rather than raising. This is the FILE-tier parser (used only by
    ``load_config``); the override tier (per-overlay / DB) uses the strict
    ``_parse_str_list`` which raises on a non-list scalar.
    """
    if not isinstance(raw, list):
        return list(_DEFAULT_DISK_CACHE_ALLOWLIST)
    return [str(s) for s in raw]


_DEFAULT_ON_BEHALF_AUTO_ACTIONS = ("post_e2e_evidence",)


def _parse_on_behalf_auto_actions(raw: object) -> list[str]:
    """Coerce the on-behalf auto-proceed allowlist, falling back to the default carve-out.

    A missing key (``None``) yields the curated default (``post_e2e_evidence`` —
    the user's own E2E evidence posts auto-proceed); an explicit list (even
    empty) is honoured verbatim so a user can re-gate evidence under a blocking
    mode. Non-list scalars degrade to the default rather than raising. FILE-tier
    parser (used only by ``load_config``); the override tier (per-overlay / DB)
    uses the strict ``_parse_str_list``.
    """
    if not isinstance(raw, list):
        return list(_DEFAULT_ON_BEHALF_AUTO_ACTIONS)
    return [str(s) for s in raw]


def _parse_env_bool(raw: str) -> bool:
    """Coerce a ``T3_*`` env string to a bool for ``ENV_SETTING_OVERRIDES``.

    Truthy set ``1``/``true``/``yes``/``on`` (case-insensitive); else ``False``.
    """
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# A default-ON ``T3_*`` env flag: present-and-off-value disables, anything else
# enables. Mirrors the legacy ``T3_HOOK_FETCH_TITLES`` semantics so a typo never
# silently disables the feature (the resolver only invokes this when the var is set).
def _parse_env_bool_default_on(raw: str) -> bool:
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _parse_env_positive_int(default: int) -> Callable[[str], int]:
    """A ``T3_*`` env coercer that fails SAFE to *default* on a bad value.

    Returns a parser that accepts a positive integer string and degrades to
    *default* for anything non-positive or non-integer. A pane-budget env var
    (``T3_TEAMS_MAX_PANES`` / ``T3_TEAMS_IDLE_MINUTES``) must never silently
    disable the safety bound by parsing to ``0`` or raising into the resolver —
    the conservative bound cannot be configured away by a typo.
    """

    def parse(raw: str) -> int:
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return parse


def _parse_env_str_list(raw: str) -> list[str]:
    """Coerce a ``T3_*`` comma-separated env string to ``list[str]`` for the env tier.

    Splits on commas and trims each token; an empty string (or a string of only
    separators/whitespace) yields ``[]`` — so ``T3_ON_BEHALF_AUTO_ACTIONS=""``
    clears the allowlist rather than reading as one empty action.
    """
    return [token for token in (part.strip() for part in raw.split(",")) if token]


def _parse_env_teams_display(raw: str) -> TeamsDisplay:
    """Coerce a ``T3_TEAMS_DISPLAY`` env string, failing SAFE to ``NONE`` (#1838 WI-5).

    The presentation-only display mode must never crash the config resolver or
    escalate itself ON via a typo in the env tier: a mistyped value degrades to
    the conservative :attr:`TeamsDisplay.NONE` (no display, in-process path
    unchanged). This is the env-tier counterpart to :meth:`TeamsDisplay.parse`,
    which raises LOUD for the TOML/DB tiers where a write-time validator catches
    the typo at set time.
    """
    try:
        return TeamsDisplay.parse(raw)
    except ValueError:
        return TeamsDisplay.NONE


def _parse_strict_bool(raw: object) -> bool:
    """Coerce a TOML/JSON value for a bool-typed overridable setting, strictly.

    TOML ``true``/``false`` and JSON ``true``/``false`` both decode to a real
    Python ``bool``, so the only accepted inputs are :data:`True` / :data:`False`
    (``isinstance(x, bool)`` — which excludes ``1``/``0`` since those are ``int``).

    Anything else — a quoted ``"false"`` (a ``str``), a number, a list — raises
    ``ValueError`` rather than truthy-coercing via ``bool(...)``. The naive
    ``bool`` coercer the bool registry entries used to point at made
    ``bool("false") == True`` (#258): a JSON/string ``"false"`` for an opt-in
    safety setting (e.g. ``allow_destructive_disk``) silently ENABLED it. This
    strict parser is the single coercer for every bool-typed overridable
    setting, so both the write path (``config_setting set`` validates through the
    registry) and the read path (``_db_setting_overrides`` coerces through it)
    reject the ambiguous value identically.
    """
    if isinstance(raw, bool):
        return raw
    msg = f"Invalid bool value {raw!r}; expected a JSON/TOML boolean (true/false), not a quoted string or number"
    raise ValueError(msg)


def _parse_strict_int(raw: object) -> int:
    """Coerce a TOML/JSON value for an int-typed overridable setting, strictly.

    Accepts a real ``int`` (TOML/JSON integer) and a numeric ``str`` (the read
    tier may store ``"5"``). REJECTS a ``bool`` — ``bool`` is a subclass of
    ``int``, so the old bare ``int`` parser made ``int(True) == 1`` and silently
    accepted a JSON ``true`` for an int-typed setting (#258), persisting the raw
    ``True``. Also rejects a ``float`` and any other non-int-coercible type
    rather than truncating (a TOML ``5.0`` for an int setting is a type error,
    raising ``TypeError``).
    The ``isinstance(raw, bool)`` guard runs BEFORE the ``int`` coercion so the
    bool short-circuits to a raise. Single coercer for every int-typed
    overridable setting, applied identically on the write and read paths.
    """
    if isinstance(raw, bool):
        msg = f"Invalid int value {raw!r}; a boolean is not an integer setting value"
        raise TypeError(msg)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        return int(raw.strip())
    msg = f"Invalid int value {raw!r}; expected a JSON/TOML integer"
    raise TypeError(msg)


def _parse_overridable_positive_int(default: int) -> Callable[[object], int]:
    """An overridable-int coercer that fails SAFE to *default* (mirrors ``_parse_env_positive_int``).

    Used for the pane-budget settings (``teams_max_panes`` / ``teams_idle_minutes``)
    in ``OVERLAY_OVERRIDABLE_SETTINGS``: a per-overlay or DB-tier value that is
    non-positive, a ``bool``, a ``float``, or a non-numeric string degrades to
    *default* rather than raising into the config resolver. The safety bound the
    setting encodes cannot be disabled by a mistyped override.
    """

    def parse(raw: object) -> int:
        if isinstance(raw, bool):
            return default
        if isinstance(raw, int):
            return raw if raw > 0 else default
        if isinstance(raw, str):
            try:
                value = int(raw.strip())
            except ValueError:
                return default
            return value if value > 0 else default
        return default

    return parse


def _parse_strict_float(raw: object) -> float:
    """Coerce a TOML/JSON value for a float-typed overridable setting, strictly.

    Accepts a real ``float``, an ``int`` (a TOML ``25`` for a float setting is
    legitimate), and a numeric ``str``. REJECTS a ``bool`` — ``float(True) ==
    1.0`` would otherwise silently accept a JSON ``true`` for a float setting,
    the same coercion-instead-of-reject class as the int parser (#258). The
    ``isinstance(raw, bool)`` guard runs BEFORE the ``int``/``float`` checks.
    Single coercer for every float-typed overridable setting, applied
    identically on the write and read paths.
    """
    if isinstance(raw, bool):
        msg = f"Invalid float value {raw!r}; a boolean is not a float setting value"
        raise TypeError(msg)
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        return float(raw.strip())
    msg = f"Invalid float value {raw!r}; expected a JSON/TOML number"
    raise TypeError(msg)


def _parse_strict_str(raw: object) -> str:
    """Coerce a TOML/JSON value for a str-typed overridable setting, strictly.

    Accepts only a real ``str``; REJECTS a ``bool``/``int``/``float``/``list``
    rather than stringifying it via ``str(...)`` (#258). The bare ``str`` parser
    accepted anything (``str(True) == "True"``, ``str(5) == "5"``), so a
    type-mismatched value for a str-typed setting was silently coerced into a
    nonsense string instead of being rejected. Single coercer for every
    str-typed overridable setting, applied identically on the write and read
    paths.
    """
    if not isinstance(raw, str):
        msg = f"Invalid str value {raw!r}; expected a JSON/TOML string"
        raise TypeError(msg)
    return raw


def _parse_user_identity_aliases(raw: object) -> list[str]:
    """Coerce a TOML list of usernames/handles to ``list[str]``.

    Returns a deduped list of non-empty alias handles, in insertion order.
    A non-list SCALAR raises ``TypeError`` (#258) — a scalar for a list-typed
    setting is a type error that must be loud, never silently degraded to an
    empty list (which would mask a corrupt override). Consumed by the ticket-disposition
    scanner (#975) to suppress reassign signals between the operator's own
    identities, and by the loop's PR/MR scanners (#976) to union-query each
    alias so cross-forge work surfaces in the statusline.
    """
    if not isinstance(raw, list):
        msg = f"Invalid user_identity_aliases value {raw!r}; expected a JSON/TOML array, not a scalar"
        raise TypeError(msg)
    return list(dict.fromkeys(str(s) for s in raw if isinstance(s, str) and s))


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
    "mode": Mode.parse,
    "autonomy": Autonomy.parse,
    "speed": Speed.parse,
    "agent_runtime": AgentRuntime.parse,
    "contribute": _parse_strict_bool,
    "excluded_skills": _parse_str_list,
    "loop_cadence_seconds": _parse_strict_int,
    "dedicated_loops": _parse_strict_bool,
    "teams_enabled": _parse_strict_bool,
    "teams_max_panes": _parse_overridable_positive_int(1),
    "teams_idle_minutes": _parse_overridable_positive_int(30),
    "teams_display": TeamsDisplay.parse,
    "require_human_approval_to_merge": _parse_strict_bool,
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
    "colleague_repo_url_pattern": _parse_strict_str,
    "solo_repo_url_pattern": _parse_strict_str,
    "require_anti_vacuity_attestation": _parse_strict_bool,
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
    "mr_title_regex": _parse_strict_str,
    "issue_implementer_enabled": _parse_strict_bool,
    "issue_implementer_label": _parse_strict_str,
    "issue_implementer_max_concurrent": _parse_strict_int,
    "issue_implementer_cadence_hours": _parse_strict_int,
    "auto_disposition_enabled": _parse_strict_bool,
    "auto_disposition_max_closes_per_tick": _parse_strict_int,
    "orchestrate_claim_enabled": _parse_strict_bool,
    # #1775 newly-DB-home (formerly file-only): these now resolve from the DB store.
    "agent_signature": _parse_strict_bool,
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
}

# TOML-home keys that ALSO support a per-overlay ``[overlays.<name>]`` override.
# A TOML-home field's authoritative tier is the TOML tables + env (never the DB),
# but a subset of them is per-overlay overridable in TOML. ``speak`` is omitted —
# its sub-table merges bespoke (see ``_overlay_speak_override``); the others in the
# carve-out are global-only. Discovery parses the union of this and the DB-home
# registry so a ``[overlays.<name>]`` value for either is read off the table; the
# resolver then keeps only TOML-home override keys.
TOML_OVERLAY_OVERRIDABLE_SETTINGS: dict[str, Callable[[Any], Any]] = {
    "orchestrator_bash_gate_enabled": _parse_strict_bool,
    "privacy": _parse_strict_str,
}

# ``T3_*`` env vars that win over both the per-overlay override and the
# global setting. Mapped to ``(UserSettings field, parser)``.
ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
    "T3_SPEED": ("speed", Speed.parse),
    "T3_AGENT_RUNTIME": ("agent_runtime", AgentRuntime.parse),
    "T3_ON_BEHALF_POST_MODE": ("on_behalf_post_mode", OnBehalfPostMode.parse),
    "T3_MISSING_ISSUE_POLICY": ("missing_issue_ref_policy", MissingIssuePolicy.parse),
    "T3_ON_BEHALF_AUTO_ACTIONS": ("on_behalf_auto_actions", _parse_env_str_list),
    "T3_REVIEW_SKILL": ("review_skill", str),
    "T3_ISSUE_IMPLEMENTER_ENABLED": ("issue_implementer_enabled", _parse_env_bool),
    "T3_LOOP_AUTO_UPDATE": ("auto_update_reinstall", _parse_env_bool),
    "T3_ORCHESTRATE_CLAIM_ENABLED": ("orchestrate_claim_enabled", _parse_env_bool),
    "T3_DEDICATED_LOOPS": ("dedicated_loops", _parse_env_bool),
    "T3_TEAMS_ENABLED": ("teams_enabled", _parse_env_bool),
    "T3_TEAMS_MAX_PANES": ("teams_max_panes", _parse_env_positive_int(1)),
    "T3_TEAMS_IDLE_MINUTES": ("teams_idle_minutes", _parse_env_positive_int(30)),
    "T3_TEAMS_DISPLAY": ("teams_display", _parse_env_teams_display),
    "T3_CONTRIBUTE": ("contribute_plugin_dir", _parse_env_bool),
    "T3_HOOK_FETCH_TITLES": ("hook_fetch_titles", _parse_env_bool_default_on),
    "T3_AUTOLOAD": ("autoload", _parse_env_bool),
}


# The irreducible bootstrap set (#1775): settings that must be readable BEFORE
# Django — and therefore the DB — is available, so they can never move into the
# ``ConfigSetting`` store. The publish gate reads ``private_repos`` from the raw
# ``[teatree]`` toml with ``tomllib`` in a hook that runs with no Django;
# ``DATABASE_URL`` / ``data_dir`` / ``DJANGO_SETTINGS_MODULE`` are the env/toml
# keys the settings module itself needs to even OPEN the DB. This typed allowlist
# is the single machine-checked home for that boundary (replacing the former
# prose-only docstring): the disjoint-registries invariant
# ``BOOTSTRAP_FILE_ONLY_SETTINGS ∩ OVERLAY_OVERRIDABLE_SETTINGS == ∅`` (a fitness
# function in the tests) makes it
# impossible to make a bootstrap key DB-overridable without turning a test red,
# and ``config-setting set`` already refuses every key here (none is in the
# overridable registry) so an admin can never stash a DB row for a file-only
# setting.
BOOTSTRAP_FILE_ONLY_SETTINGS: frozenset[str] = frozenset(
    {
        "DATABASE_URL",
        "data_dir",
        "DJANGO_SETTINGS_MODULE",
        "private_repos",
    }
)


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


def _default_handover_mirror_path() -> Path:
    """Human-readable mirror of the latest session hand-off.

    ``${XDG_STATE_HOME:-~/.local/state}/teatree/handover/latest.md`` — XDG
    *state* (not data) because a hand-off is regenerable transient session
    state, not durable user data. Overridable via ``[teatree]
    handover_mirror_path``. The DB row is the source of truth; this file
    is for human-readability and for bootstrapping a brand-new session.
    """
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "teatree" / "handover" / "latest.md"


@dataclass
class UserSettings:
    workspace_dir: Path = field(default_factory=lambda: Path.home() / "workspace")
    worktrees_dir: Path = field(default_factory=lambda: DATA_DIR / "worktrees")
    privacy: str = ""
    check_updates: bool = True
    # #256 Default-OFF teatree engagement. When false (the default) a fresh
    # Claude session does NOT auto-engage teatree — no skill auto-suggest, no
    # PreToolUse load-block, no loop scheduling — and SessionStart shows a
    # one-line how-to-start advisory instead. The owner flips it true to
    # auto-activate every session. TOML-home like ``check_updates`` (the cold
    # SessionStart / UserPromptSubmit hooks read it pre-Django, so it can never
    # move into the DB store); ``T3_AUTOLOAD`` env wins. A DB row is ignored on
    # read. Explicitly calling ``/teatree`` — or loading any ``t3:`` skill —
    # engages teatree for the session regardless of this default.
    autoload: bool = False
    timezone: str = ""
    contribute: bool = False
    excluded_skills: list[str] = field(default_factory=list)
    mode: Mode = Mode.INTERACTIVE
    autonomy: Autonomy = Autonomy.BABYSIT
    # The single runtime selector for loop-dispatched phase agents (those whose
    # (role, phase) has a registered phase sub-agent). ``interactive`` (default,
    # today's behaviour) dispatches them in-session via the ``/loop`` slot's
    # ``Agent`` tool; ``sdk_oauth`` / ``sdk_apikey`` / ``api`` run them headless
    # via ``agents/headless.py`` (OAuth subscription / metered API key / future
    # raw-API runner). Per-overlay overridable; ``T3_AGENT_RUNTIME`` env wins.
    agent_runtime: AgentRuntime = AgentRuntime.INTERACTIVE
    # How much parallel work the orchestrator drives at once. The
    # conservative ``MEDIUM`` baseline means NO orchestrator fan-out — only
    # the intrinsic loop + PR sweep + per-overlay ``max_concurrent_auto_starts``
    # provide throughput. ``slow`` caps to one impl worker; ``full`` arms the
    # /t3:speed loop; ``boost`` runs a single parallel-blast wave. Orthogonal
    # to ``mode``/``autonomy`` (those gate *whether* a publish proceeds; this
    # governs *how many* threads run) and never relaxes a safety gate.
    # Per-overlay overridable; ``T3_SPEED`` env wins over both.
    speed: Speed = Speed.MEDIUM
    # Loop tick interval in seconds (BLUEPRINT § 5.6). Default 12 minutes.
    loop_cadence_seconds: int = 720
    # Opt-in: replace the single fat `loop-owner` slot (one `run_tick`
    # fanning across ALL mini-loops) with N dedicated `/loop <cadence>`
    # slots, each driving one dedicated loop (a named group of mini-loops)
    # via a SCOPED `t3 loop tick --slot <name>` claiming `loop:<name>`
    # (#1838 Track-A). Default OFF and fail-OFF: when false the slot
    # generator emits the single fat slot and the no-`--slot` tick path is
    # byte-identical to today (BLUEPRINT § 5.6 "Per-loop owning-session
    # layer"). Per-overlay overridable; `T3_DEDICATED_LOOPS` env wins.
    dedicated_loops: bool = False
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
    # Training-wheel for `auto` overlays: when true, the loop autonomously
    # pushes and creates PRs but stops short of merging — merge requires a
    # human reaction (👍 or `/merge`). The user flips this off only once
    # comfortable (BLUEPRINT § 5.6.2). No effect in `interactive` mode,
    # where every publishing action prompts regardless.
    require_human_approval_to_merge: bool = True
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
    # Pass --chrome to every spawned `claude` session so Claude in Chrome is
    # available wherever it could be useful (browser inspection, UI debugging,
    # E2E selector drafting, bug hunts). Costs ~300 lines of system prompt per
    # session; turn off only on machines without the Chrome extension.
    claude_chrome: bool = True
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
    # `teatree.notify.notify_user(...)` posts agent answers / questions /
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
    # ON) with a weekly cadence (168h). Companion to the `backlog-sweep`
    # skill: once the sweep's verdicts prove trustworthy the loop fires a
    # low-frequency `backlog_sweep` task that triages the issue backlog
    # (superseded / stale / regressive / still-valid against current
    # `main`). The sweep is destructive-capable — it can propose closing
    # issues — so unlike the always-on news/eval scanners the kill switch
    # defaults ON: the scanner stays inert until the user sets
    # ``backlog_sweep_disabled = false`` in ``[teatree]`` (or per-overlay).
    backlog_sweep_disabled: bool = True
    backlog_sweep_skill: str = "backlog-sweep"
    backlog_sweep_cadence_hours: int = 168
    # #2419 Ask-gate for backlog-sweep issue closes. When true (default),
    # the backlog-sweep skill must NOT mass-close issues unattended — it
    # records each close proposal with its citation and surfaces the batch
    # to the user, closing only on explicit approval. Only the
    # high-confidence merged-PR-superseded class auto-closes. Default ON:
    # an unattended wrong close destroys backlog signal, the failure mode
    # this gate forecloses. Per-overlay overridable.
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
    disk_cache_allowlist: list[str] = field(
        default_factory=lambda: ["~/.cache/pre-commit", "~/.cache/puppeteer", "~/.cache/codex-runtimes"],
    )
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
    # Default ``0`` keeps the legacy unbounded behaviour so the gate is
    # opt-in; set ``1`` (or any positive integer) to enforce the cap.
    # Per-overlay overridable: a heavy overlay can cap to ``1`` while a
    # cheap dogfood overlay stays unbounded.
    max_concurrent_local_stacks: int = 0
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
    # #2060 The resolved [teatree.speak] sub-table — a local playback enum
    # (off/dm/all) + a slack bool. See :class:`SpeakConfig` + blueprint §10.1.1.
    speak: SpeakConfig = field(default_factory=SpeakConfig)
    # The resolved [mr_reminder] slug→channel routing table for the
    # cross-repo "my open MRs" reminder; empty default keeps it inert.
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
    colleague_repo_url_pattern: str = ""
    solo_repo_url_pattern: str = ""
    # Conventional-Commits title pattern enforced at ``pr create`` BEFORE the
    # gh/glab network call (#1540). A non-matching title is rejected with the
    # pattern printed verbatim; the description is independently required to
    # carry a What/Why header. Per-overlay overridable via
    # ``[overlays.<name>].mr_title_regex = "…"`` so an overlay with a different
    # title grammar declares its own pattern without flipping the global.
    mr_title_regex: str = DEFAULT_MR_TITLE_REGEX
    # #1548 Opt-in, default-OFF gate for the always-on issue-implementer
    # loop. The loop is a hard NO-OP unless ``issue_implementer_enabled``
    # is flipped on, mirroring the ``review_skill = ""`` opt-in (#1541) and
    # the ``scanning_news_*`` cadence pattern. This PR adds only the config
    # surface — the scanner and dispatch land in later PRs.
    issue_implementer_enabled: bool = False
    # Label marking an issue as auto-implement. Empty means no issue is
    # ever dispatched even when the loop is enabled (defence-in-depth: both
    # the master gate AND a non-empty label are required before any work
    # is picked up).
    issue_implementer_label: str = ""
    # Cap on simultaneously in-flight auto-implement tickets.
    issue_implementer_max_concurrent: int = 1
    # Internal dispatch-rate floor (hours) between auto-implement pickups.
    issue_implementer_cadence_hours: int = 1
    # #1796 / agent-teams Track-A PR#1: opt-in, default-OFF arm for the
    # dispatch loop's ``orchestrate_phase`` claim. The phase is wired dormant
    # (``claim=False``) in ``run_tick`` — it computes the deterministic fan-out
    # manifest from ``speed`` + ``max_concurrent_auto_starts`` but never claims
    # or spawns. When this is flipped on, the tick runs ``orchestrate_phase``
    # with ``claim=True`` so the lead does the thin per-unit claim+spawn the
    # manifest already computes (the #786-N4 claim-is-the-spawn boundary). When
    # off (the default) the dormant ``claim=False`` path is kept EXACTLY, so the
    # fat loop's behaviour is unchanged. Mirrors ``issue_implementer_enabled``;
    # per-overlay overridable and ``T3_ORCHESTRATE_CLAIM_ENABLED`` env wins over
    # both.
    orchestrate_claim_enabled: bool = False
    # #2122 Opt-in, default-OFF gate for the issue-disposition triage scanner.
    # When False (the default) no scanner is built, so the loop emits nothing
    # and never auto-closes an issue. The scanner only CLOSES high-confidence
    # dead noise (already-shipped / exact-duplicate / obsolete) — it is
    # physically unable to enqueue work, so flipping it on cannot grow the
    # backlog queue.
    auto_disposition_enabled: bool = False
    # Upper bound on close-candidate signals emitted per tick — keeps an
    # auto-close pass bounded and reviewable.
    auto_disposition_max_closes_per_tick: int = 5
    # Human-readable mirror of the latest session hand-off. The
    # ``SessionHandover`` DB row is the source of truth; this file mirrors
    # the payload for human-readability and for bootstrapping a brand-new
    # session. Default ``${XDG_STATE_HOME:-~/.local/state}/teatree/handover/
    # latest.md``; override via ``[teatree] handover_mirror_path``.
    handover_mirror_path: Path = field(default_factory=_default_handover_mirror_path)
    # Env kill-switch ``T3_ISSUE_IMPLEMENTER_ENABLED`` (operational fast-
    # disable) wins over both the per-overlay override and the global
    # setting; resolution is env → per-overlay ``[overlays.<name>]`` →
    # global ``[teatree]`` → this dataclass default.
    # SDK-equivalent cost reporting (``t3 cost``). Day-of-month the Agent-SDK
    # monthly credit refreshes; the billing cycle ``t3 cost`` totals against
    # starts on that day. ``0`` (default) means the refresh day is unknown, so
    # the cycle is the calendar month. ``sdk_monthly_credit_usd`` is the credit
    # the cycle-to-date spend is shown against ($200 = Max 20x).
    billing_cycle_anchor_day: int = 0
    sdk_monthly_credit_usd: float = 200.0
    # #2697 — formerly env-only bypass readers, now DB-home (#1775): each resolves
    # from the ``ConfigSetting`` store + its ``T3_*`` env layer where one is
    # registered in ``ENV_SETTING_OVERRIDES``, never from a bespoke
    # ``os.environ.get`` read. Set via ``t3 <overlay> config_setting set <key>``.
    #
    # GitLab-approval poll scanner (formerly ``TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED``).
    # Default off — poll-driven and overlapping with the webhook path.
    gitlab_approval_scanner_enabled: bool = False
    # Pass ``--plugin-dir`` to the launched Claude Code agent so retro may edit
    # core plugin files (formerly ``T3_CONTRIBUTE``). ``T3_CONTRIBUTE`` env wins.
    contribute_plugin_dir: bool = False
    # Enable the dream command's eval-proposal phase on the manual ``run`` path
    # (formerly ``T3_DREAM_PROPOSE_EVALS``). The cadence-driven ``tick`` path has
    # its own seam and does not route through this field.
    dream_propose_evals: bool = False
    # Fetch PR/issue titles to enrich a prompt before trigger matching (formerly
    # ``T3_HOOK_FETCH_TITLES``). Default on. ``T3_HOOK_FETCH_TITLES`` env wins;
    # the UserPromptSubmit hook runs pre-Django, so there the DB tier is skipped
    # (fail-safe) and env + this default govern — identical to legacy behaviour.
    hook_fetch_titles: bool = True


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)
