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
    "scanning_news_disabled": bool,
    "scanning_news_skill": str,
    "scanning_news_cadence_hours": int,
}

# ``T3_*`` env vars that win over both the per-overlay override and the
# global setting. Mapped to ``(UserSettings field, parser)``.
ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
    "T3_ON_BEHALF_POST_MODE": ("on_behalf_post_mode", OnBehalfPostMode.parse),
}


@dataclass
class OverlayEntry:
    name: str
    overlay_class: str
    project_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


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
    # #1191 Periodic scanning-news scanner — CORE always-on with a daily
    # cadence (24h). Companion to the `scanning-news` skill (#1190): the
    # loop fires a `scanning_news` task daily so the news-scan workflow
    # runs without depending on an external cron. Set
    # ``scanning_news_disabled = true`` in ``[teatree]`` (or per-overlay)
    # as the escape hatch.
    scanning_news_disabled: bool = False
    scanning_news_skill: str = "scanning-news"
    scanning_news_cadence_hours: int = 24


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)


def load_config(path: Path | None = None) -> TeaTreeConfig:
    if path is None:
        path = CONFIG_PATH
    if not path.is_file():
        return TeaTreeConfig()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    teatree = raw.get("teatree", {})
    workspace_dir = Path(teatree.get("workspace_dir", "~/workspace")).expanduser()
    worktrees_dir = Path(teatree.get("worktrees_dir", str(DATA_DIR / "worktrees"))).expanduser()

    raw_excluded = teatree.get("excluded_skills", [])
    excluded_skills = [str(s) for s in raw_excluded] if isinstance(raw_excluded, list) else []

    toml_mode = teatree.get("mode")
    mode = Mode.parse(toml_mode) if toml_mode is not None else Mode.INTERACTIVE

    on_behalf_post_mode, ask_before_post_on_behalf = _resolve_on_behalf_post_mode(teatree)

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
        scanning_news_disabled=bool(teatree.get("scanning_news_disabled", False)),
        scanning_news_skill=str(teatree.get("scanning_news_skill", "scanning-news")),
        scanning_news_cadence_hours=int(teatree.get("scanning_news_cadence_hours", 24)),
    )

    return TeaTreeConfig(user=user, raw=raw)


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


def get_effective_settings() -> UserSettings:
    """Return the user settings with env and per-overlay overrides applied.

    Resolution per field (first match wins): ``T3_*`` env var (see
    ``ENV_SETTING_OVERRIDES``), active overlay's override from
    ``[overlays.<name>]``, global ``[teatree]`` value, ``UserSettings``
    dataclass default.

    The active overlay is resolved via ``T3_OVERLAY_NAME`` first (matches
    ``get_overlay()``), then cwd-based discovery, then the single
    installed overlay.

    To make an additional setting overridable, add it to
    ``OVERLAY_OVERRIDABLE_SETTINGS`` (per-overlay) or
    ``ENV_SETTING_OVERRIDES`` (env). The resolver picks it up generically
    via ``dataclasses.replace`` — no per-setting getter glue required.
    Callers read the effective value with ``get_effective_settings().X``.
    """
    base = load_config().user
    active = _active_overlay_entry()
    overrides: dict[str, Any] = dict(active.overrides) if active is not None else {}
    for env_var, (field_name, parser) in ENV_SETTING_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is not None:
            overrides[field_name] = parser(raw)
    if not overrides:
        return base
    return replace(base, **overrides)


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
