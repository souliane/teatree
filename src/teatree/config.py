"""TeaTree configuration — overlay discovery from ~/.teatree.toml."""

import importlib.util
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".teatree.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "teatree"


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
    workspace_dir: Path = field(default_factory=lambda: Path.home() / "workspace")
    worktrees_dir: Path = field(default_factory=lambda: DATA_DIR / "worktrees")
    branch_prefix: str = ""
    privacy: str = ""
    check_updates: bool = True
    timezone: str = ""


@dataclass
class TeaTreeConfig:
    user: UserSettings = field(default_factory=UserSettings)
    raw: dict = field(default_factory=dict)


def load_config(path: Path = CONFIG_PATH) -> TeaTreeConfig:
    if not path.is_file():
        return TeaTreeConfig()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    teatree = raw.get("teatree", {})
    workspace_dir = Path(teatree.get("workspace_dir", "~/workspace")).expanduser()
    worktrees_dir = Path(teatree.get("worktrees_dir", str(DATA_DIR / "worktrees"))).expanduser()

    user = UserSettings(
        workspace_dir=workspace_dir,
        worktrees_dir=worktrees_dir,
        branch_prefix=teatree.get("branch_prefix", ""),
        privacy=teatree.get("privacy", ""),
        check_updates=teatree.get("check_updates", True),
        timezone=teatree.get("timezone", ""),
    )

    return TeaTreeConfig(user=user, raw=raw)


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


def check_for_updates(*, force: bool = False) -> str | None:
    """Check PyPI/GitHub for a newer teatree release.

    Returns a human-readable upgrade message, or ``None`` when already
    up-to-date (or when update checks are disabled in user settings and
    *force* is ``False``).

    Results are cached for 24 h in ``DATA_DIR / "update-check.json"``.
    """
    import json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415, S404
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
        result = subprocess.run(
            ["gh", "api", "repos/souliane/teatree/releases/latest", "--jq", ".tag_name"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        tag = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
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
