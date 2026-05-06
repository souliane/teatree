"""TeaTree configuration — overlay discovery from ~/.teatree.toml."""

import importlib.util
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

CONFIG_PATH = Path.home() / ".teatree.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "teatree"


class Mode(StrEnum):
    """Operating mode for agent sessions.

    ``interactive`` (default, conservative on security) gates publishing actions
    on explicit user approval — push, MR creation/merge, external writes all
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


def get_data_dir(namespace: str) -> Path:
    """Return the data directory for a given namespace, creating it if needed."""
    data_dir = DATA_DIR / namespace
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


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
}

# ``T3_*`` env vars that win over both the per-overlay override and the
# global setting. Mapped to ``(UserSettings field, parser)``.
ENV_SETTING_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "T3_MODE": ("mode", Mode.parse),
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
    # Pass --chrome to every spawned `claude` session so Claude in Chrome is
    # available wherever it could be useful (browser inspection, UI debugging,
    # E2E selector drafting, bug hunts). Costs ~300 lines of system prompt per
    # session; turn off only on machines without the Chrome extension.
    claude_chrome: bool = True
    # Whether teatree should append an agent identity (`Co-Authored-By`,
    # "Sent using …", "Generated with …") to artifacts published on the
    # user's behalf — git commits, MR/PR descriptions and comments, Slack
    # messages, issue bodies. Default off: the user is the author, the agent
    # is the typist. Honored by every teatree post-on-behalf code path; the
    # rule for ad-hoc agent posting (MCP Slack, gh comment, etc.) lives in
    # `skills/rules/SKILL.md` § "No AI Signature on Posts Made on the User's
    # Behalf".
    agent_signature: bool = False


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
        claude_chrome=bool(teatree.get("claude_chrome", True)),
        agent_signature=bool(teatree.get("agent_signature", False)),
    )

    return TeaTreeConfig(user=user, raw=raw)


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
    """Check PyPI/GitHub for a newer teatree release.

    Returns a human-readable upgrade message, or ``None`` when already
    up-to-date (or when update checks are disabled in user settings and
    *force* is ``False``).

    Results are cached for 24 h in ``DATA_DIR / "update-check.json"``.
    """
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    config = load_config()
    if not force and not config.user.check_updates:
        return None

    cache_path = DATA_DIR / "update-check.json"
    ttl = 86_400  # 24 h

    # Return cached result when still fresh.
    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - cached.get("ts", 0) < ttl:
                return cached.get("message") or None
        except (json.JSONDecodeError, OSError):
            pass

    import importlib.metadata  # noqa: PLC0415

    current = importlib.metadata.version("teatree")

    try:
        result = run_allowed_to_fail(
            ["gh", "api", "repos/souliane/teatree/releases/latest", "--jq", ".tag_name"],
            expected_codes=None,
            timeout=10,
        )
        tag = result.stdout.strip()
    except (TimeoutExpired, FileNotFoundError):
        return None

    if not tag:
        return None

    latest = tag.lstrip("v")
    if latest == current:
        _write_update_cache(cache_path, "")
        return None

    message = f"teatree {tag} available (you have {current}). Run: uv pip install --upgrade teatree"
    _write_update_cache(cache_path, message)
    return message


def _write_update_cache(cache_path: Path, message: str) -> None:
    """Persist the update-check result so we don't hit the network every invocation."""
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"ts": time.time(), "message": message}),
        encoding="utf-8",
    )


def discover_overlays(config_path: Path | None = None) -> list[OverlayEntry]:
    """Discover overlays from ~/.teatree.toml and installed entry points.

    Sources (merged by name, toml wins on conflict):
    1. ``[overlays.<name>]`` sections in the toml config (``path`` key)
    2. ``teatree.overlays`` entry-point group from installed packages
    """
    from importlib.metadata import entry_points  # noqa: PLC0415

    if config_path is None:
        config_path = CONFIG_PATH
    seen: dict[str, OverlayEntry] = {}

    # 1. Toml config
    config = load_config(config_path)
    for name, overlay_cfg in config.raw.get("overlays", {}).items():
        overlay_class = overlay_cfg.get("class", "")
        path_str = overlay_cfg.get("path", "")
        project_path = Path(path_str).expanduser() if path_str else None
        if not overlay_class and project_path:
            # Backward compat: derive settings module from manage.py for TOML overlays
            manage_py = project_path / "manage.py"
            settings_module = _extract_settings_module(manage_py) if manage_py.is_file() else ""
            # Store settings module as overlay_class fallback so callers can still use it
            overlay_class = settings_module
        overrides: dict[str, Any] = {}
        for key, parser in OVERLAY_OVERRIDABLE_SETTINGS.items():
            if key in overlay_cfg:
                overrides[key] = parser(overlay_cfg[key])
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
