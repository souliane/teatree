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
    settings_module: str
    project_path: Path | None = None


@dataclass
class TeaTreeConfig:
    workspace_dir: Path = field(default_factory=lambda: Path.home() / "workspace")
    branch_prefix: str = ""
    privacy: str = ""
    raw: dict = field(default_factory=dict)


def load_config(path: Path = CONFIG_PATH) -> TeaTreeConfig:
    if not path.is_file():
        return TeaTreeConfig()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    teatree = raw.get("teatree", {})
    workspace_dir = Path(teatree.get("workspace_dir", "~/workspace")).expanduser()

    return TeaTreeConfig(
        workspace_dir=workspace_dir,
        branch_prefix=teatree.get("branch_prefix", ""),
        privacy=teatree.get("privacy", ""),
        raw=raw,
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
        path = Path(overlay_cfg.get("path", "")).expanduser()
        manage_py = path / "manage.py"
        settings_module = _extract_settings_module(manage_py) if manage_py.is_file() else ""
        seen[name] = OverlayEntry(name=name, settings_module=settings_module, project_path=path)

    # 2. Entry points (skip if already found via toml)
    for ep in entry_points(group="teatree.overlays"):
        if ep.name not in seen:
            seen[ep.name] = OverlayEntry(
                name=ep.name,
                settings_module=ep.value,
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
                return OverlayEntry(name=directory.name, settings_module=settings_module, project_path=directory)
    return None


def _resolve_ep_project_path(settings_module: str) -> Path | None:
    """Resolve the project root for an entry-point overlay from its settings module.

    Locates the top-level package on disk, then walks up to find a ``manage.py``
    — the same marker used by TOML and cwd-based discovery.
    """
    top_package = settings_module.split(".", maxsplit=1)[0]
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
