"""Initialization module — called once per script invocation.

Loads defaults, auto-detects frameworks, and registers them.
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
