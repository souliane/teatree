"""TeaTree configuration — overlay discovery from ~/.teatree.toml."""

import importlib.util
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from teatree.paths import DATA_DIR, get_data_dir
from teatree.types import DEFAULT_MR_TITLE_REGEX, SlackVoiceClassifierMode, SpeakMode, SpeakTarget
from teatree.update_check import run_update_check

CONFIG_PATH = Path.home() / ".teatree.toml"


class Mode(StrEnum):
    """Operating mode for agent sessions.

    ``interactive`` (default, conservative on security) gates publishing actions
    on explicit user approval — push, PR creation/merge, external writes all
    stop and ask. ``auto`` grants full autonomy: the agent ships end-to-end
    without confirmation, falling back to interactive only for the non-
    negotiable always-gated list (force-push to default branches, destructive
    shared-state ops). Opt in via ``[teatree] mode = "auto"`` in
    ``~/.teatree.toml`` or the ``T3_MODE`` environment variable.
    """

    INTERACTIVE = "interactive"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str) -> "Mode":
        """Parse a mode string. Invalid values raise ``ValueError``.

        The conservative default (``INTERACTIVE``) is applied by the caller
        when the setting is absent — this function only validates explicit
        values, so typos never silently downgrade to a less-safe mode.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid t3 mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


# Friendly aliases accepted by ``Speed.parse`` and normalised to a canonical
# tier. Module-level (not a class attribute) so ``StrEnum`` does not try to
# treat the mapping as an enum member.
_SPEED_ALIASES: dict[str, str] = {
    "low": "slow",
    "normal": "medium",
    "high": "full",
}


class Speed(StrEnum):
    """How much parallel work the orchestrator drives at once.

    A single dial spanning sequential to burst throughput. Orthogonal to
    :class:`Mode` and :class:`Autonomy` (which govern *whether* a publishing
    action may proceed); ``speed`` governs *how many* threads of work run
    concurrently — it never relaxes a safety gate.

    Tiers (``SLOW`` < ``MEDIUM`` < ``FULL`` < ``BOOST``, default ``MEDIUM``):

    *   :attr:`SLOW` — at most one implementation worker in flight at a time
        (the cold-review reviewer still runs separately). The cautious dial
        for a fragile tree or a constrained host.
    *   :attr:`MEDIUM` — the conservative baseline: NO orchestrator fan-out.
        Throughput comes only from the intrinsic loop, the PR sweep, and the
        per-overlay ``max_concurrent_auto_starts`` auto-start cap.
    *   :attr:`FULL` — arm ``/loop /t3:speed boost`` so each wave re-classifies
        the backlog and fans out a burst, sustained across waves.
    *   :attr:`BOOST` — one parallel-backlog-blast wave (the former
        ``/t3:full-speed`` behaviour), clamped to ``max_concurrent_auto_starts``.

    A no-arg ``/t3:speed`` invocation means "go full" regardless of the
    persisted baseline; the persisted value is the resting dial the loop
    reads. Opt in via ``[teatree] speed = "full"`` in ``~/.teatree.toml``,
    the ``T3_SPEED`` environment variable, or ``t3 teatree speed set <level>``.
    """

    SLOW = "slow"
    MEDIUM = "medium"
    FULL = "full"
    BOOST = "boost"

    @classmethod
    def parse(cls, value: str) -> "Speed":
        """Parse a speed string, accepting friendly aliases; typos raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default (:attr:`MEDIUM`)
        is applied by the caller when the setting is absent, so this validates
        only explicit values and a typo never silently changes throughput.
        ``low``/``normal``/``high`` map onto ``slow``/``medium``/``full``.
        """
        normalised = value.strip().lower()
        normalised = _SPEED_ALIASES.get(normalised, normalised)
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            aliases = ", ".join(sorted(_SPEED_ALIASES))
            msg = f"Invalid speed {value!r}; valid values: {valid} (aliases: {aliases})"
            raise ValueError(msg) from exc


class Autonomy(StrEnum):
    """The single per-overlay trust switch collapsing the three user-approval gates.

    Tiers (``FULL`` > ``NOTIFY`` > ``BABYSIT``, default ``BABYSIT``):

    *   :attr:`BABYSIT` — every approval gate keeps its own value; the user
        stays in the loop on merges, answers, and colleague-visible posts.
    *   :attr:`NOTIFY` — autonomous, but every on-behalf action DMs the user
        (derived ``notify_on_behalf``) and the user's MR merges only after a
        colleague approval (per-diff CLEAR, never self-approve).
    *   :attr:`FULL` — autonomous with no after-the-fact DM; the single-author
        ``solo_overlay`` merge bypass is reachable here only, and the substrate
        per-PR sign-off is satisfied by this standing grant (the §17.4.3 step 5
        carve-out — see :func:`teatree.core.merge_execution.assert_merge_preconditions`)
        so a substrate CLEAR needs no per-CLEAR ``human_authorizer``.

    Both autonomous tiers collapse the three gates and pin ``mode = auto`` (see
    :func:`_apply_autonomy`). An explicit per-gate value always wins. The
    safety floor (privacy/leak gate, cold-review with reviewer != maker,
    CI-green, not-draft, never-lockout, the SHA-bound audited keystone
    transition) is out of scope and never touched — under ``full`` the substrate
    carve-out removes ONLY the per-PR human sign-off, never a floor guard.
    """

    BABYSIT = "babysit"
    NOTIFY = "notify"
    FULL = "full"

    @classmethod
    def parse(cls, value: str) -> "Autonomy":
        """Parse an autonomy string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`BABYSIT`) is applied by the caller when the setting is
        absent, so a typo never silently grants full autonomy.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid autonomy {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class OnBehalfPostMode(StrEnum):
    """Tri-state pre-gate over on-behalf colleague/customer posts (#960).

    Three points on the autonomy ramp for posts the agent makes *as the
    user* to a colleague/customer surface (PR/MR comment, issue comment,
    Slack channel/thread post, Notion post, PR/MR approve, reaction on
    someone else's message):

    *   :attr:`DRAFT_OR_ASK` (default) — colleague-invisible, revocable
        draft notes (``t3 review post-draft-note``) publish autonomously
        and the agent DMs the user with publish/delete commands; every
        other gated action collapses to BLOCK, identical to :attr:`ASK`.
        The user gets autonomous draft-note posting (drafts are not visible
        to colleagues until explicitly published) without yielding control
        over any other colleague-visible mutation.
    *   :attr:`ASK` — every gated action requires an explicit recorded
        approval (``t3 review approve-on-behalf <target> <action>
        --approver <id>``) before it publishes.
    *   :attr:`IMMEDIATE` — the gate is off; gated actions publish
        directly (subject to the always-gated list in :class:`Mode`).

    The user satisfies the gate without a TTY by recording an
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`;
    DMs *to the user themselves* and internal-only orchestration writes
    are out of scope and remain ungated under every mode.
    """

    DRAFT_OR_ASK = "draft_or_ask"
    ASK = "ask"
    IMMEDIATE = "immediate"

    @classmethod
    def parse(cls, value: str) -> "OnBehalfPostMode":
        """Parse an on-behalf-post-mode string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`DRAFT_OR_ASK`) is applied by the caller when the setting
        is absent, so typos never silently downgrade to a less-safe
        mode.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid on_behalf_post_mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


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


@dataclass
class E2ERepo:
    """An external git repository containing Playwright E2E tests."""

    name: str
    url: str
    branch: str
    e2e_dir: str = "e2e"


def _parse_excluded_skills(raw: object) -> list[str]:
    return [str(s) for s in raw] if isinstance(raw, list) else []


_DEFAULT_DISK_CACHE_ALLOWLIST = ("~/.cache/pre-commit", "~/.cache/puppeteer", "~/.cache/codex-runtimes")


def _parse_disk_cache_allowlist(raw: object) -> list[str]:
    """Coerce the disk cache allow-list, falling back to the regenerable-cache default.

    A missing key (``None``) yields the curated default set of regenerable
    caches; an explicit list (even empty) is honoured verbatim so a user can
    narrow the allow-list to nothing. Non-list scalars degrade to the default
    rather than raising.
    """
    if raw is None:
        return list(_DEFAULT_DISK_CACHE_ALLOWLIST)
    if not isinstance(raw, list):
        return list(_DEFAULT_DISK_CACHE_ALLOWLIST)
    return [str(s) for s in raw]


def _parse_env_bool(raw: str) -> bool:
    """Coerce a ``T3_*`` env-var string to a bool for ``ENV_SETTING_OVERRIDES``.

    Conservative truthy set (``1``/``true``/``yes``/``on``, case-insensitive);
    everything else — including ``false``/``0``/``no`` — resolves to ``False``.
    A kill-switch env var is meant to *disable*, so any value that is not an
    explicit enable reads as off.
    """
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_user_identity_aliases(raw: object) -> list[str]:
    """Coerce a TOML list of usernames/handles to ``list[str]``.

    Returns a deduped list of non-empty alias handles, in insertion order.
    Non-list inputs (a stray scalar) degrade to an empty list rather than
    raising — keeps the loader robust to a malformed override while leaving
    the suppression off by default. Consumed by the ticket-disposition
    scanner (#975) to suppress reassign signals between the operator's own
    identities, and by the loop's PR/MR scanners (#976) to union-query each
    alias so cross-forge work surfaces in the statusline.
    """
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(s) for s in raw if isinstance(s, str) and s))


# Registry of UserSettings fields that can be overridden per-overlay in
# ``[overlays.<name>]``. To make another setting overridable, add an entry
# here with a parser that coerces the raw toml value to the UserSettings
# field type. The getter `get_effective_settings()` applies overrides
# generically via ``dataclasses.replace`` — no per-setting wiring needed.
OVERLAY_OVERRIDABLE_SETTINGS: dict[str, Callable[[Any], Any]] = {
    "mode": Mode.parse,
    "autonomy": Autonomy.parse,
    "speed": Speed.parse,
    "branch_prefix": str,
    "privacy": str,
    "contribute": bool,
    "excluded_skills": _parse_excluded_skills,
    "loop_cadence_seconds": int,
    "require_human_approval_to_merge": bool,
    "require_human_approval_to_answer": bool,
    "ask_before_post_on_behalf": bool,
    "on_behalf_post_mode": OnBehalfPostMode.parse,
    "notify_user_via_bot": bool,
    "notify_on_post_on_behalf": bool,
    "user_identity_aliases": _parse_user_identity_aliases,
    "architectural_review_disabled": bool,
    "architectural_review_skill": str,
    "architectural_review_cadence_hours": int,
    "architectural_review_after_merge_count": int,
    "review_skill": str,
    "require_review_context": bool,
    "scanning_news_disabled": bool,
    "scanning_news_skill": str,
    "scanning_news_cadence_hours": int,
    "ask_before_creating_news_tickets": bool,
    "eval_local_disabled": bool,
    "eval_local_skill": str,
    "eval_local_cadence_hours": int,
    "dogfood_smoke_disabled": bool,
    "dogfood_smoke_skill": str,
    "dogfood_smoke_cadence_hours": int,
    "dogfood_smoke_overlay": str,
    "self_update_disabled": bool,
    "self_update_cadence_hours": int,
    "auto_update_reinstall": bool,
    "auto_update_require_green_main": bool,
    "resource_pressure_disabled": bool,
    "resource_pressure_cadence_minutes": int,
    "resource_pressure_min_free_interval_minutes": int,
    "disk_warn_free_gb": float,
    "disk_crit_free_gb": float,
    "ram_warn_avail_gb": float,
    "ram_crit_avail_gb": float,
    "disk_cache_allowlist": _parse_excluded_skills,
    "allow_destructive_disk": bool,
    "worktree_stale_days": int,
    "max_worktree_gc_per_tick": int,
    "allow_destructive_ram": bool,
    "ram_kill_allowlist": _parse_excluded_skills,
    "todo_sweep_disabled": bool,
    "todo_sweep_recheck_interval_hours": int,
    "max_concurrent_local_stacks": int,
    "slack_voice_classifier_mode": SlackVoiceClassifierMode.parse,
    "speak_mode": SpeakMode.parse,
    "speak_target": SpeakTarget.parse,
    "pull_main_clone_disabled": bool,
    "pull_main_clone_cadence_hours": int,
    "review_nag_enabled": bool,
    "orchestrator_bash_gate_enabled": bool,
    "mr_title_regex": str,
    "issue_implementer_enabled": bool,
    "issue_implementer_label": str,
    "issue_implementer_max_concurrent": int,
    "issue_implementer_cadence_hours": int,
}

# ``T3_*`` env vars that win over both the per-overlay override and the
# global setting. Mapped to ``(UserSettings field, parser)``.
ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
    "T3_SPEED": ("speed", Speed.parse),
    "T3_ON_BEHALF_POST_MODE": ("on_behalf_post_mode", OnBehalfPostMode.parse),
    "T3_REVIEW_SKILL": ("review_skill", str),
    "T3_ISSUE_IMPLEMENTER_ENABLED": ("issue_implementer_enabled", _parse_env_bool),
    "T3_LOOP_AUTO_UPDATE": ("auto_update_reinstall", _parse_env_bool),
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
    branch_prefix: str = ""
    privacy: str = ""
    check_updates: bool = True
    timezone: str = ""
    contribute: bool = False
    excluded_skills: list[str] = field(default_factory=list)
    redis_db_count: int = 16
    mode: Mode = Mode.INTERACTIVE
    autonomy: Autonomy = Autonomy.BABYSIT
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
    # Pre-gate for posts the agent makes under the user's identity to a
    # colleague/customer surface (PR/MR comment, issue comment, Slack
    # channel/thread post, Notion post, PR/MR approve, reaction on
    # someone else's message).
    #
    # **Deprecated** in favour of the tri-state ``on_behalf_post_mode``
    # below. Kept on ``UserSettings`` for one release as a derived
    # computed value: ``True`` when the resolved mode is ``ASK`` or
    # ``DRAFT_OR_ASK``, ``False`` when ``IMMEDIATE``. The toml loader
    # still accepts ``[teatree] ask_before_post_on_behalf = true/false``
    # and translates it into the new mode (see ``load_config``) so
    # existing user configs keep working.
    ask_before_post_on_behalf: bool = True
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
    # Backward-compat shim: if ``on_behalf_post_mode`` is absent but the
    # legacy ``ask_before_post_on_behalf`` is set, the loader resolves
    # the mode as ``ASK`` (true) / ``IMMEDIATE`` (false). The default
    # when neither is set is ``DRAFT_OR_ASK``.
    on_behalf_post_mode: OnBehalfPostMode = OnBehalfPostMode.DRAFT_OR_ASK
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
    # publish and never blocks or rolls back the post. Flip off via
    # `[teatree] notify_on_post_on_behalf = false`; per-overlay
    # overridable; intentionally NO env var (notify_user_via_bot, its
    # sibling, has none — a copied-by-analogy env layer would be a lie).
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
    # reviewing-phase evidence gate (``teatree.core.review_skill_gate``) is
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
    # #129 TODO-sweep scanner — per-overlay; verifies open Task rows against
    # their artifact's terminal state (issue closed / PR merged) and completes
    # only on durable proof, never in bulk and never on a stale read. On by
    # default; ``todo_sweep_disabled = true`` is the escape hatch.
    # ``todo_sweep_recheck_interval_hours`` is the per-task anti-thrash window
    # (a task swept within it is skipped this tick) and the idempotency window
    # for the atomic ``last_sweep_check_ts`` stamp.
    todo_sweep_disabled: bool = False
    todo_sweep_recheck_interval_hours: int = 1
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
    # #1791 What is read aloud — see :class:`SpeakMode` + blueprint §10.1.1.
    speak_mode: SpeakMode = SpeakMode.OFF
    # #1791 Where spoken audio lands — see :class:`SpeakTarget` + §10.1.1.
    speak_target: SpeakTarget = SpeakTarget.LOCAL
    # #1398 Pre-publish close-trailer scanner. fnmatch patterns over
    # ``namespace/repo``: when an MR/PR target repo matches one of these
    # patterns and the body carries a ``Closes|Fixes|Resolves`` trailer,
    # the trailer line is silently stripped before publishing. Default
    # empty preserves legacy behaviour. Parsed from
    # ``[teatree.publish_gates] ban_close_trailers_on_namespaces``.
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


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)


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


def load_config(path: Path | None = None) -> TeaTreeConfig:
    if path is None:
        path = CONFIG_PATH
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
        autonomy=_resolve_autonomy(teatree),
        speed=_resolve_speed(teatree),
        loop_cadence_seconds=int(teatree.get("loop_cadence_seconds", 720)),
        require_human_approval_to_merge=bool(teatree.get("require_human_approval_to_merge", True)),
        require_human_approval_to_answer=bool(teatree.get("require_human_approval_to_answer", True)),
        ask_before_post_on_behalf=ask_before_post_on_behalf,
        on_behalf_post_mode=on_behalf_post_mode,
        notify_user_via_bot=bool(teatree.get("notify_user_via_bot", True)),
        notify_on_post_on_behalf=bool(teatree.get("notify_on_post_on_behalf", True)),
        claude_chrome=bool(teatree.get("claude_chrome", True)),
        agent_signature=bool(teatree.get("agent_signature", False)),
        statusline_chain=[str(s) for s in teatree.get("statusline_chain", [])],
        user_identity_aliases=_parse_user_identity_aliases(teatree.get("user_identity_aliases", [])),
        repo_mode=str(teatree.get("repo_mode", "")),
        architectural_review_disabled=bool(teatree.get("architectural_review_disabled", False)),
        architectural_review_skill=str(teatree.get("architectural_review_skill", "ac-reviewing-codebase")),
        architectural_review_cadence_hours=int(teatree.get("architectural_review_cadence_hours", 168)),
        architectural_review_after_merge_count=int(teatree.get("architectural_review_after_merge_count", 25)),
        review_skill=str(teatree.get("review_skill", "")),
        require_review_context=bool(teatree.get("require_review_context", False)),
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
        ram_kill_allowlist=_parse_excluded_skills(teatree.get("ram_kill_allowlist", [])),
        todo_sweep_disabled=bool(teatree.get("todo_sweep_disabled", False)),
        todo_sweep_recheck_interval_hours=int(teatree.get("todo_sweep_recheck_interval_hours", 1)),
        max_concurrent_local_stacks=int(teatree.get("max_concurrent_local_stacks", 0)),
        slack_voice_classifier_mode=_resolve_slack_voice_classifier_mode(teatree),
        speak_mode=_resolve_speak_mode(teatree),
        speak_target=_resolve_speak_target(teatree),
        ban_close_trailers_on_namespaces=ban_close_trailers_on_namespaces,
        pull_main_clone_disabled=bool(teatree.get("pull_main_clone_disabled", False)),
        pull_main_clone_cadence_hours=int(teatree.get("pull_main_clone_cadence_hours", 1)),
        review_nag_enabled=bool(teatree.get("review_nag_enabled", False)),
        orchestrator_bash_gate_enabled=bool(teatree.get("orchestrator_bash_gate_enabled", True)),
        mr_title_regex=str(teatree.get("mr_title_regex", DEFAULT_MR_TITLE_REGEX)),
        issue_implementer_enabled=bool(teatree.get("issue_implementer_enabled", False)),
        issue_implementer_label=str(teatree.get("issue_implementer_label", "")),
        issue_implementer_max_concurrent=int(teatree.get("issue_implementer_max_concurrent", 1)),
        issue_implementer_cadence_hours=int(teatree.get("issue_implementer_cadence_hours", 1)),
        handover_mirror_path=(
            Path(str(teatree["handover_mirror_path"])).expanduser()
            if teatree.get("handover_mirror_path")
            else _default_handover_mirror_path()
        ),
        billing_cycle_anchor_day=int(teatree.get("billing_cycle_anchor_day", 0)),
        sdk_monthly_credit_usd=float(teatree.get("sdk_monthly_credit_usd", 200.0)),
    )

    return TeaTreeConfig(user=user, raw=raw)


def _resolve_slack_voice_classifier_mode(teatree: dict[str, Any]) -> SlackVoiceClassifierMode:
    """Resolve ``slack_voice_classifier_mode`` from ``[teatree]`` (#1395).

    Accepts either a flat key ``[teatree] slack_voice_classifier_mode``
    or a nested ``[teatree.publish_gates] slack_voice_classifier_mode``
    (the table the issue brief sketches for grouping future
    pre-publish gates). The flat key wins when both are present;
    falling back through the nested table then to the conservative
    default keeps the backward-compat upgrade path clean — existing
    configs that don't know about the gate inherit ``WARN`` (log the
    mismatch, allow the post) rather than ``STRICT`` (refuse).
    """
    flat = teatree.get("slack_voice_classifier_mode")
    if flat is not None:
        return SlackVoiceClassifierMode.parse(flat)
    nested = teatree.get("publish_gates")
    if isinstance(nested, dict):
        scoped = nested.get("slack_voice_classifier_mode")
        if scoped is not None:
            return SlackVoiceClassifierMode.parse(scoped)
    return SlackVoiceClassifierMode.WARN


def _resolve_speak_mode(teatree: dict[str, Any]) -> SpeakMode:
    """Resolve ``speak_mode`` from a flat ``[teatree] speak_mode`` key (#1791).

    Absent → :attr:`SpeakMode.OFF` (the feature ships disabled). A typo
    surfaces a clean ``ValueError`` from :meth:`SpeakMode.parse` rather
    than silently mis-resolving. This is the CONFIGURED value only; the
    binary-presence gate that forces ``off`` when ``say`` is absent lives
    in :func:`teatree.core.speak.resolve_mode` so the prerequisite check
    is applied at the single egress seam, not duplicated in the loader.
    """
    raw = teatree.get("speak_mode")
    return SpeakMode.parse(raw) if raw is not None else SpeakMode.OFF


def _resolve_speak_target(teatree: dict[str, Any]) -> SpeakTarget:
    """Resolve ``speak_target`` from a flat ``[teatree] speak_target`` key (#1791).

    Absent → :attr:`SpeakTarget.LOCAL` (the zero-dependency macOS default).
    A typo surfaces a clean ``ValueError`` from :meth:`SpeakTarget.parse`.
    """
    raw = teatree.get("speak_target")
    return SpeakTarget.parse(raw) if raw is not None else SpeakTarget.LOCAL


def _resolve_autonomy(teatree: dict[str, Any]) -> Autonomy:
    """Resolve the global ``autonomy`` switch from a ``[teatree]`` toml table.

    Absent → the conservative :attr:`Autonomy.BABYSIT`; a typo raises via
    :meth:`Autonomy.parse` (never a silent grant of full autonomy). The
    per-overlay override is applied later in :func:`get_effective_settings`.
    """
    raw = teatree.get("autonomy")
    return Autonomy.parse(raw) if raw is not None else Autonomy.BABYSIT


def _resolve_speed(teatree: dict[str, Any]) -> Speed:
    """Resolve the global ``speed`` dial from a ``[teatree]`` toml table.

    Absent → the conservative :attr:`Speed.MEDIUM`; a typo raises via
    :meth:`Speed.parse` (never a silent throughput change). The per-overlay
    override and the ``T3_SPEED`` env var are applied later in
    :func:`get_effective_settings`.
    """
    raw = teatree.get("speed")
    return Speed.parse(raw) if raw is not None else Speed.MEDIUM


def _resolve_on_behalf_post_mode(teatree: dict[str, Any]) -> tuple[OnBehalfPostMode, bool]:
    """Resolve ``on_behalf_post_mode`` from a ``[teatree]`` toml table.

    Precedence:

    1.  Explicit ``on_behalf_post_mode = "..."`` always wins.
    2.  Legacy ``ask_before_post_on_behalf = true/false`` maps to
        :attr:`OnBehalfPostMode.ASK` / :attr:`OnBehalfPostMode.IMMEDIATE`.
    3.  Neither set → :attr:`OnBehalfPostMode.DRAFT_OR_ASK` (new default).

    Returns ``(mode, derived_ask_bool)`` so the legacy boolean field on
    ``UserSettings`` stays consistent with the resolved mode for the one
    deprecation release we keep it around.
    """
    raw_mode = teatree.get("on_behalf_post_mode")
    if raw_mode is not None:
        mode = OnBehalfPostMode.parse(raw_mode)
    elif "ask_before_post_on_behalf" in teatree:
        # Backward-compat alias: explicit legacy boolean → matching mode.
        legacy = bool(teatree["ask_before_post_on_behalf"])
        mode = OnBehalfPostMode.ASK if legacy else OnBehalfPostMode.IMMEDIATE
    else:
        mode = OnBehalfPostMode.DRAFT_OR_ASK
    # Derived legacy boolean: ASK/DRAFT_OR_ASK both block colleague-visible
    # publishing (only the draft-form variant publishes autonomously under
    # DRAFT_OR_ASK), so they map to "ask before post" = True.
    derived_ask = mode is not OnBehalfPostMode.IMMEDIATE
    return mode, derived_ask


def load_e2e_repos(path: Path | None = None) -> list[E2ERepo]:
    """Load named E2E repos from ``[e2e_repos.<name>]`` sections in ``~/.teatree.toml``.

    Each entry may specify ``url``, ``branch``, and optionally ``e2e_dir``
    (the subdirectory containing ``playwright.config.ts``, default ``"e2e"``).
    """
    config = load_config(path)
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
    return load_config().user.workspace_dir


def worktrees_dir() -> Path:
    """Canonical worktrees directory (where ticket worktrees are created)."""
    from django.conf import settings  # noqa: PLC0415

    if hasattr(settings, "T3_WORKTREES_DIR"):
        return Path(settings.T3_WORKTREES_DIR)
    return load_config().user.worktrees_dir


def get_effective_settings(overlay_name: str | None = None) -> UserSettings:
    """Return the user settings with env and per-overlay overrides applied.

    Resolution per field (first match wins): ``T3_*`` env var (see
    ``ENV_SETTING_OVERRIDES``), active overlay's override from
    ``[overlays.<name>]``, global ``[teatree]`` value, ``UserSettings``
    dataclass default.

    The active overlay is resolved via ``T3_OVERLAY_NAME`` first (matches
    ``get_overlay()``), then cwd-based discovery, then the single
    installed overlay.

    ``overlay_name`` resolves a SPECIFIC named overlay instead of the active
    one — the loop's scanner-builders fan out over every registered overlay,
    not just the session's. In that mode the env layer is NOT applied (``T3_*``
    vars target the active session's overlay, not an arbitrary named one); the
    per-overlay ``[overlays.<name>]`` overrides and the autonomy collapse run
    identically. This is the single resolver both paths share, so the loop's
    auto-merge / codex consumers see the SAME ``autonomy`` posture the active
    path exposes.

    To make an additional setting overridable, add it to
    ``OVERLAY_OVERRIDABLE_SETTINGS`` (per-overlay) or
    ``ENV_SETTING_OVERRIDES`` (env). The resolver picks it up generically
    via ``dataclasses.replace`` — no per-setting getter glue required.
    Callers read the effective value with ``get_effective_settings().X``.

    As a final step, the single ``autonomy`` switch is applied: when
    the effective autonomy resolves to :attr:`Autonomy.FULL` or
    :attr:`Autonomy.NOTIFY`, the three user-in-the-loop approval gates
    collapse to their autonomous value and ``mode`` is pinned to ``auto`` —
    unless the user pinned a gate explicitly (an explicit per-gate value
    always wins). The ``notify`` tier additionally derives
    ``notify_on_behalf = True`` (forces the after-receipt DM on). See
    :func:`_apply_autonomy`.
    """
    config = load_config()
    base = config.user
    overrides = _overlay_overrides_by_name(overlay_name) if overlay_name is not None else _active_overlay_overrides()
    settings = base if not overrides else replace(base, **overrides)
    return _apply_autonomy(settings, hard_pinned=set(overrides), global_pinned=_global_pinned_fields(config))


def _active_overlay_overrides() -> dict[str, Any]:
    """Per-overlay overrides for the active overlay, with the env layer applied."""
    active = _active_overlay_entry()
    overrides: dict[str, Any] = dict(active.overrides) if active is not None else {}
    for env_var, (field_name, parser) in ENV_SETTING_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is not None:
            overrides[field_name] = parser(raw)
    return overrides


def _overlay_overrides_by_name(overlay_name: str) -> dict[str, Any]:
    """Per-overlay overrides for a NAMED overlay (no env layer — see caller).

    The match is canonical-alias-tolerant: a request for the short alias
    ``teatree`` resolves the ``t3-``-prefixed entry-point overlay's
    ``[overlays.t3-teatree]`` overrides, and vice versa. ``ticket.overlay``
    and ``infer_overlay_for_url`` return the entry-point name while older
    rows / configs may carry the bare alias; an exact-name-only match would
    silently drop the per-overlay values (and an autonomous overlay would
    resolve to ``babysit``).
    """
    canonical = OverlayEntry.canonical_overlay_name(overlay_name)
    for entry in discover_overlays():
        if not entry.overrides:
            continue
        if entry.name == overlay_name or OverlayEntry.canonical_overlay_name(entry.name) == canonical:
            return dict(entry.overrides)
    return {}


# User-approval gates only (never the safety floor); value each collapses to under an autonomous tier.
_AUTONOMY_COLLAPSED_GATE_VALUES: dict[str, Any] = {
    "on_behalf_post_mode": OnBehalfPostMode.IMMEDIATE,
    "require_human_approval_to_merge": False,
    "require_human_approval_to_answer": False,
}

# ``babysit`` is absent: it collapses nothing.
_AUTONOMOUS_TIERS: frozenset[Autonomy] = frozenset({Autonomy.NOTIFY, Autonomy.FULL})


def _global_pinned_fields(config: TeaTreeConfig) -> set[str]:
    """Names of settings explicitly set in the global ``[teatree]`` toml table.

    A *global* explicit value is a deliberate per-gate opinion for the three
    approval gates and still wins over the autonomy collapse — except for
    ``mode``: a global ``[teatree] mode`` is a workspace-wide default, not a
    statement about an autonomous overlay, so it must NOT defeat the autonomy
    ``mode = auto`` pin (a common ``mode = "interactive"`` global would
    otherwise leave a ``full``/``notify`` overlay half-autonomous — gates
    relaxed but the merge path still gated on ``mode == AUTO``). A *per-overlay*
    ``[overlays.<name>].mode`` arrives via the override layer (``hard_pinned``)
    and DOES win — see :func:`_apply_autonomy`.
    """
    teatree = config.raw.get("teatree", {})
    return set(teatree) if isinstance(teatree, dict) else set()


def _apply_autonomy(settings: UserSettings, *, hard_pinned: set[str], global_pinned: set[str]) -> UserSettings:
    """Collapse the three approval gates for an autonomous tier (``full`` / ``notify``).

    Both autonomous tiers fill only the gates the user left unpinned and pin
    ``mode`` to ``auto`` (the merge-autonomy path is gated on ``mode == AUTO``,
    so a ``full``/``notify`` overlay that forgot ``mode`` would otherwise be a
    silent no-op). The ``notify`` tier additionally derives
    ``notify_on_behalf = True`` so every on-behalf action DMs the user.
    ``babysit`` is a no-op — every gate keeps its resolved value.

    Pin precedence:

    *   For the three approval gates, an explicit pin of EITHER kind
        (``hard_pinned`` = env / per-overlay override, or ``global_pinned`` =
        a global ``[teatree]`` key) wins — a deliberate opinion is never
        silently overridden.
    *   For ``mode`` only, a global ``[teatree] mode`` does NOT win (it is a
        workspace default, not an opinion about this overlay); only a
        ``hard_pinned`` per-overlay/env ``mode`` keeps the user's value. This
        is the over-pin fix: a common global ``mode = "interactive"`` no longer
        leaves an autonomous overlay half-collapsed.

    The safety floor is untouched: only the keys in
    :data:`_AUTONOMY_COLLAPSED_GATE_VALUES` (plus ``mode`` and the derived
    ``notify_on_behalf``) are ever written here.
    """
    if settings.autonomy not in _AUTONOMOUS_TIERS:
        return settings
    gate_pinned = hard_pinned | global_pinned
    relaxed: dict[str, Any] = {
        field_name: value
        for field_name, value in _AUTONOMY_COLLAPSED_GATE_VALUES.items()
        if field_name not in gate_pinned
    }
    if "mode" not in hard_pinned:
        relaxed["mode"] = Mode.AUTO
    if settings.autonomy is Autonomy.NOTIFY and "notify_on_behalf" not in gate_pinned:
        relaxed["notify_on_behalf"] = True
    if not relaxed:
        return settings
    return replace(settings, **relaxed)


def cadence_seconds() -> int:
    """Resolve the loop slot cadence in seconds (minimum 60s).

    This setting is not registered in ``ENV_SETTING_OVERRIDES`` — its env
    layer is a bespoke direct read, so its resolution does NOT go through
    the generic effective-settings env layer. Layers, first match wins:
    first the ``T3_LOOP_CADENCE`` env var (the bespoke direct read), then
    ``get_effective_settings().loop_cadence_seconds`` which covers the
    per-overlay ``[overlays.<name>]`` override, then the global
    ``[teatree]`` value in ``~/.teatree.toml``, then the ``UserSettings``
    default of 720.

    Any ``T3_LOOP_CADENCE`` parse failure falls back to 720. The result is
    clamped to a 60s minimum so a misconfigured tiny value cannot busy-loop
    the tick.
    """
    raw = os.environ.get("T3_LOOP_CADENCE")
    if raw is not None and raw.strip():
        try:
            return max(60, int(raw.strip()))
        except ValueError:
            return 720
    return max(60, get_effective_settings().loop_cadence_seconds)


def _active_overlay_entry() -> OverlayEntry | None:
    """Find the active overlay's toml entry (carrying any overrides).

    Prefers ``T3_OVERLAY_NAME`` (the same env var ``get_overlay()`` uses)
    to avoid worktree-dir/overlay-name mismatch.
    """
    overlays = discover_overlays()
    by_name = {entry.name: entry for entry in overlays}

    name = os.environ.get("T3_OVERLAY_NAME")
    if name and name in by_name:
        return by_name[name]

    fallback = discover_active_overlay()
    if fallback is not None and fallback.name in by_name:
        # The cwd-based lookup returns a bare OverlayEntry without overrides;
        # swap in the toml entry so override parsing applies.
        return by_name[fallback.name]

    if len(overlays) == 1:
        return overlays[0]

    return None


def check_for_updates(*, force: bool = False) -> str | None:
    """Resolve a "new release available" notice from config + update_check.

    Reads ``check_updates`` from user config and delegates to
    :func:`teatree.update_check.run_update_check`. The implementation
    lives in :mod:`teatree.update_check` (split out for module-health
    LOC); this wrapper is the config-aware entry point.
    """
    return run_update_check(check_updates=load_config().user.check_updates, force=force)


def discover_overlays(config_path: Path | None = None) -> list[OverlayEntry]:
    """Discover overlays from ~/.teatree.toml and installed entry points.

    Sources (merged by name, toml wins on conflict):
    1. ``[overlays.<name>]`` sections in the toml config (``path`` key)
    2. ``teatree.overlays`` entry-point group from installed packages

    A bare config-only ``[overlays.<alias>]`` table (no ``path``/``class``)
    whose name is a legacy short alias of an installed entry-point overlay
    is folded into that canonical entry-point overlay rather than emitted
    as a separate one — older ``slack-bot`` runs wrote ``[overlays.teatree]``
    for the ``t3-teatree`` overlay, which made discovery list both as if
    they were distinct overlays (souliane/teatree#1108).
    """
    from importlib.metadata import entry_points  # noqa: PLC0415

    if config_path is None:
        config_path = CONFIG_PATH
    seen: dict[str, OverlayEntry] = {}

    ep_names = {ep.name for ep in entry_points(group="teatree.overlays")}

    # 1. Toml config
    config = load_config(config_path)
    for name, overlay_cfg in config.raw.get("overlays", {}).items():
        overlay_class = overlay_cfg.get("class", "")
        path_str = overlay_cfg.get("path", "")
        project_path = Path(path_str).expanduser() if path_str else None
        overrides: dict[str, Any] = {}
        for key, parser in OVERLAY_OVERRIDABLE_SETTINGS.items():
            if key in overlay_cfg:
                overrides[key] = parser(overlay_cfg[key])
        if not overlay_class and project_path is None and name not in ep_names:
            canonical = _match_canonical_ep(name, ep_names)
            if canonical is not None:
                # Legacy short-alias config table — fold its overrides into
                # the canonical entry-point overlay below; do not emit a
                # stray overlay under the alias name.
                continue
        if not overlay_class and project_path:
            manage_py = project_path / "manage.py"
            settings_module = _extract_settings_module(manage_py) if manage_py.is_file() else ""
            overlay_class = settings_module
        seen[name] = OverlayEntry(
            name=name,
            overlay_class=overlay_class,
            project_path=project_path,
            overrides=overrides,
        )

    # 2. Entry points (skip if already found via toml)
    for ep in entry_points(group="teatree.overlays"):
        if ep.name not in seen:
            seen[ep.name] = OverlayEntry(
                name=ep.name,
                overlay_class=ep.value,
                project_path=_resolve_ep_project_path(ep.value),
            )

    return list(seen.values())


def _match_canonical_ep(alias: str, ep_names: "set[str]") -> str | None:
    """Return the canonical overlay name a short ``alias`` maps to.

    Single home for the legacy-alias rule (souliane/teatree#1138): a bare
    ``[overlays.<alias>]`` table in ``~/.teatree.toml`` (without
    ``path``/``class``) maps to the installed overlay whose name equals
    ``alias`` or ends with ``"-<alias>"`` — e.g. a short
    ``[overlays.teatree]`` table folds into the canonical
    ``t3-teatree`` entry point.

    The dash separator in the suffix match is required: a name that
    happens to end with the alias *without* a dash (e.g. ``t3acme``
    for alias ``acme``) is a semantic collision, not a legacy alias,
    and is rejected. Returns ``None`` when no canonical match exists.
    """
    for ep_name in ep_names:
        if ep_name == alias or ep_name.endswith(f"-{alias}"):
            return ep_name
    return None


def discover_active_overlay() -> OverlayEntry | None:
    """Find the overlay to use.

    Priority:
    1. manage.py in cwd ancestors (developer workflow)
    2. Single installed overlay (end-user workflow)
    """
    local = _discover_from_manage_py()
    if local:
        return local

    installed = discover_overlays()
    if len(installed) == 1:
        return installed[0]

    return None


def _discover_from_manage_py() -> OverlayEntry | None:
    """Walk up from cwd to find a manage.py and extract its settings module."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        manage_py = directory / "manage.py"
        if manage_py.is_file():
            settings_module = _extract_settings_module(manage_py)
            if settings_module:
                return OverlayEntry(name=directory.name, overlay_class="", project_path=directory)
    return None


def _resolve_ep_project_path(overlay_class: str) -> Path | None:
    """Resolve the project root for an entry-point overlay from its class path.

    ``overlay_class`` is e.g. ``"teatree.contrib.t3_teatree.overlay:TeatreeOverlay"``.
    Parses the module part (before the ``:``) to find the top-level package on disk,
    then walks up to find a ``manage.py`` — the same marker used by TOML and cwd-based
    discovery.
    """
    module_path = overlay_class.split(":", maxsplit=1)[0]
    top_package = module_path.split(".", maxsplit=1)[0]
    spec = importlib.util.find_spec(top_package)
    if spec is None or not spec.submodule_search_locations:
        return None
    pkg_dir = Path(spec.submodule_search_locations[0])
    for parent in [pkg_dir, *pkg_dir.parents]:
        if (parent / "manage.py").is_file():
            return parent
    return None


def _extract_settings_module(manage_py: Path) -> str:
    text = manage_py.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "DJANGO_SETTINGS_MODULE" in line and '"' in line:
            return line.split('"')[-2]
    return ""
