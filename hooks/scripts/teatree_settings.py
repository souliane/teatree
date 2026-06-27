"""Shared ``[teatree] <flag>`` boolean-setting reader for the hook leaves (#2746).

Extracted from ``hook_router`` so a leaf gate (e.g. ``memory_recall``) can read its
own kill-switch WITHOUT importing ``hook_router`` — which would create a
``hook_router`` ↔ leaf import cycle (``hook_router`` imports the leaf's handler into
its ``_HANDLERS`` chain). This is a dependency-free leaf: both ``hook_router`` and the
gate leaves import IT, and it imports nothing first-party.
"""

import os
import sys
import tomllib
from pathlib import Path

# Alias both identities so a bare ``from teatree_settings import ...`` (the live
# hook, whose dir is on sys.path) and ``hooks.scripts.teatree_settings`` (a
# subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("teatree_settings", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.teatree_settings", sys.modules[__name__])


def _load_home_toml() -> dict:
    """Best-effort parse of ``~/.teatree.toml``; ``{}`` on a missing/broken config."""
    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return {}
    return config if isinstance(config, dict) else {}


def section_bool_setting(section: str, name: str, *, default: bool) -> bool:
    """Best-effort read of a ``[section] <name>`` boolean flag from ``~/.teatree.toml``.

    The single shared shape behind every cold ``[section] <flag>`` boolean reader:
    fails to ``default`` on a missing/broken config, a missing ``[section]`` table,
    a missing key, or a non-boolean value, and returns the configured value only
    when it is a bare TOML boolean. So only a bare boolean ``false`` disables a
    ``default=True`` flag and only a bare boolean ``true`` enables a
    ``default=False`` one — a QUOTED ``"false"`` / ``"true"`` (a string, not a
    bool) is ignored and the default stands. An explicit bare boolean is the
    one-line kill-switch / opt-in, never a code edit (NEVER-LOCKOUT).
    """
    table = _load_home_toml().get(section)
    if not isinstance(table, dict):
        return default
    value = table.get(name)
    return value if isinstance(value, bool) else default


def teatree_bool_setting(name: str, *, default: bool = True) -> bool:
    """Best-effort read of a ``[teatree] <name>`` boolean flag (see :func:`section_bool_setting`)."""
    return section_bool_setting("teatree", name, default=default)


_AUTOLOAD_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def autoload_enabled() -> bool:
    """Whether teatree auto-engages a fresh session (#256). Default OFF, fail-closed.

    The cold-hook reader the SessionStart / UserPromptSubmit hooks consult to
    decide default-off engagement, structured like the loops auto-load opt-in:
    env-first (``T3_AUTOLOAD`` truthy), else ``[teatree] autoload`` via
    :func:`teatree_bool_setting`. Fails CLOSED (OFF) on a missing/broken config,
    so a fresh install never auto-engages teatree until the owner opts in.
    """
    env = os.environ.get("T3_AUTOLOAD", "").strip().lower()
    if env:
        return env in _AUTOLOAD_TRUTHY
    return teatree_bool_setting("autoload", default=False)
