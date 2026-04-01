"""TeaTree configuration — overlay discovery from ~/.teatree.toml."""

import importlib.util
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".teatree.toml"
DATA_DIR = Path.home() / ".local" / "share" / "teatree"


def get_data_dir(namespace: str) -> Path:
    """Return the data directory for a given namespace, creating it if needed."""
    data_dir = DATA_DIR / namespace
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def default_logging(namespace: str) -> dict:
    """Return a default Django LOGGING dict that writes to ``<data_dir>/logs/dashboard.log``.

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
                "filename": str(log_dir / "dashboard.log"),
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
class OverlayEntry:
    name: str
    overlay_class: str
    project_path: Path | None = None


@dataclass
class UserSettings:
    """User preferences from ``[teatree]`` in ``~/.teatree.toml``.

    These are personal preferences that affect the user's local experience
    — not overlay-specific and not framework internals.
    """

    workspace_dir: Path = field(default_factory=lambda: Path.home() / "workspace")
    worktrees_dir: Path = field(default_factory=lambda: Path.home() / ".teatree" / "worktrees")
    branch_prefix: str = ""
    privacy: str = ""
    check_updates: bool = True
    timezone: str = ""


@dataclass
class TeaTreeConfig:
    """Full parsed config including raw TOML for overlay discovery."""

    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)

    # Backward compat properties
    @property
    def workspace_dir(self) -> Path:
        return self.user.workspace_dir

    @property
    def worktrees_dir(self) -> Path:
        return self.user.worktrees_dir

    @property
    def branch_prefix(self) -> str:
        return self.user.branch_prefix


def load_config(path: Path = CONFIG_PATH) -> TeaTreeConfig:
    if not path.is_file():
        return TeaTreeConfig()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    teatree = raw.get("teatree", {})

    user = UserSettings(
        workspace_dir=Path(teatree.get("workspace_dir", "~/workspace")).expanduser(),
        worktrees_dir=Path(teatree.get("worktrees_dir", "~/.teatree/worktrees")).expanduser(),
        branch_prefix=teatree.get("branch_prefix", ""),
        privacy=teatree.get("privacy", ""),
        check_updates=teatree.get("check_updates", True),
        timezone=teatree.get("timezone", ""),
    )

    return TeaTreeConfig(user=user, raw=raw)


def discover_overlays(config_path: Path = CONFIG_PATH) -> list[OverlayEntry]:
    """Discover overlays from ~/.teatree.toml and installed entry points.

    Sources (merged by name, toml wins on conflict):
    1. ``[overlays.<name>]`` sections in the toml config (``path`` key)
    2. ``teatree.overlays`` entry-point group from installed packages
    """
    from importlib.metadata import entry_points  # noqa: PLC0415

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
        seen[name] = OverlayEntry(name=name, overlay_class=overlay_class, project_path=project_path)

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


# ── Update checker ───────────────────────────────────────────────────

_UPDATE_CACHE_FILE = DATA_DIR / "update-check.json"
_UPDATE_CHECK_INTERVAL_SECONDS = 86400  # 24 hours


def check_for_updates(*, force: bool = False) -> str | None:
    """Return an update message if a newer tag exists upstream, or None.

    Checks at most once per day (cached). Respects ``check_updates`` user setting.
    """
    import json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415, S404
    import time  # noqa: PLC0415

    config = load_config()
    if not config.user.check_updates and not force:
        return None

    # Check cache
    if not force and _UPDATE_CACHE_FILE.is_file():
        try:
            cache = json.loads(_UPDATE_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - cache.get("checked_at", 0) < _UPDATE_CHECK_INTERVAL_SECONDS:
                return cache.get("message") or None
        except (json.JSONDecodeError, OSError):
            pass

    # Fetch latest tag
    result = subprocess.run(
        ["gh", "api", "repos/souliane/teatree/releases/latest", "--jq", ".tag_name"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _write_update_cache("")
        return None

    latest_tag = result.stdout.strip()
    if not latest_tag:
        _write_update_cache("")
        return None

    # Compare with installed version
    from importlib.metadata import version  # noqa: PLC0415

    try:
        current = version("teatree")
    except Exception:  # noqa: BLE001
        current = "0.0.0"

    message = ""
    if latest_tag.lstrip("v") != current:
        message = f"teatree {latest_tag} available (you have {current}). Run: uv pip install --upgrade teatree"

    _write_update_cache(message)
    return message or None


def _write_update_cache(message: str) -> None:
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    _UPDATE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _UPDATE_CACHE_FILE.write_text(
        json.dumps({"checked_at": time.time(), "message": message}) + "\n",
        encoding="utf-8",
    )
