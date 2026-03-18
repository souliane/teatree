"""Initialization module — called once per script invocation.

Loads user config (~/.teatree), sets up sys.path, auto-detects frameworks,
and registers extension point overrides.
Project overrides are registered separately (e.g. by a project skill's hooks module).
"""

import os
import sys
from pathlib import Path

_initialized = False


def init() -> None:
    global _initialized  # noqa: PLW0603
    if _initialized:
        return
    _initialized = True

    # 0. Load user config and ensure overlay is on sys.path
    _load_teatree_config()

    # 0b. Load worktree env from the caller's directory.
    # _t3_python cd's to the scripts dir (for pyenv), which triggers direnv
    # to reload with the main repo's env — overwriting worktree vars like
    # CLIENT_NAME, DATABASE_URL, etc.  Restore them from the nearest
    # .env.worktree relative to where the user actually ran the command.
    _load_worktree_env()

    # Ensure the scripts dir is on PYTHONPATH
    scripts_dir = str(Path(__file__).resolve().parent.parent)
    if scripts_dir not in sys.path:  # pragma: no cover
        sys.path.insert(0, scripts_dir)

    # 1. Register default no-op extension points
    from lib.extension_points import register_defaults

    register_defaults()

    # 2. Auto-detect frameworks and register their overrides
    _detect_frameworks()

    # 3. Auto-detect project hooks (registered at 'project' layer, overrides frameworks)
    _detect_project_hooks()


def _load_teatree_config() -> None:
    """Load ~/.teatree config into os.environ and sys.path.

    Reads KEY=VALUE lines (supports ``"`` quoting and ``$HOME`` expansion).
    Only T3_* vars with non-empty values are loaded, using setdefault so
    env vars already set by the shell or CI are not overwritten.

    Also ensures the project overlay's scripts/ directory is on sys.path
    so _detect_project_hooks() can import it.
    """
    config = Path.home() / ".teatree"
    if not config.is_file():
        return
    home = str(Path.home())
    with config.open(encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            value = value.replace("$HOME", home)
            if key.startswith("T3_") and value:
                os.environ.setdefault(key, value)

    overlay = os.environ.get("T3_OVERLAY", "")
    if overlay:
        overlay_scripts = str(Path(overlay) / "scripts")
        if Path(overlay_scripts).is_dir() and overlay_scripts not in sys.path:
            sys.path.insert(0, overlay_scripts)


def _load_worktree_env() -> None:
    """Load the nearest .env.worktree relative to the caller's original CWD.

    _t3_python sets _T3_ORIG_CWD before cd'ing to the scripts dir.  When
    direnv reloads in the scripts dir it overwrites worktree-specific vars
    (CLIENT_NAME, DATABASE_URL, etc.) with the main repo's values.

    This function restores them by searching upward from _T3_ORIG_CWD for
    .env.worktree and force-loading it (overwriting current os.environ).
    """
    orig_cwd = os.environ.get("_T3_ORIG_CWD", "")
    if not orig_cwd:
        return
    for parent in [Path(orig_cwd), *Path(orig_cwd).parents]:
        envfile = parent / ".env.worktree"
        if envfile.is_file():
            with envfile.open(encoding="utf-8") as f:
                for raw_line in f:
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, _, value = stripped.partition("=")
                    os.environ[key.strip()] = value.strip()
            return


def _detect_project_hooks() -> None:
    """Auto-detect project hooks if their module is on PYTHONPATH.

    Project overlay skills place a ``lib/project_hooks.py`` module on
    PYTHONPATH via their ``scripts/`` directory.  The module must expose a
    ``register()`` function that installs project-layer overrides.
    """
    try:
        from lib.project_hooks import register  # ty: ignore[unresolved-import]

        register()
    except ImportError:
        pass


def _detect_frameworks() -> None:
    """Auto-detect frameworks in workspace repos and register overrides."""
    from lib.env import workspace_dir

    ws = workspace_dir()

    # Django: detected via manage.py in any workspace repo
    django_detected = False
    try:
        with os.scandir(ws) as entries:
            for entry in entries:
                if entry.is_dir() and (Path(entry.path) / "manage.py").is_file():
                    django_detected = True
                    break
    except OSError:
        pass

    if django_detected:
        from frameworks.django import register_django

        register_django()
