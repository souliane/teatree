"""Shared ``[teatree] <flag>`` boolean-setting reader for the hook leaves (#2746).

Extracted from ``hook_router`` so a leaf gate (e.g. ``memory_recall``) can read its
own kill-switch WITHOUT importing ``hook_router`` — which would create a
``hook_router`` ↔ leaf import cycle (``hook_router`` imports the leaf's handler into
its ``_HANDLERS`` chain). This is a dependency-free leaf: both ``hook_router`` and the
gate leaves import IT, and it imports nothing first-party.
"""

import sys
import tomllib
from pathlib import Path

# Alias both identities so a bare ``from teatree_settings import ...`` (the live
# hook, whose dir is on sys.path) and ``hooks.scripts.teatree_settings`` (a
# subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("teatree_settings", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.teatree_settings", sys.modules[__name__])


def teatree_bool_setting(name: str, *, default: bool = True) -> bool:
    """Best-effort read of a ``[teatree] <name>`` boolean flag from ``~/.teatree.toml``.

    The single shared shape behind every ``[teatree] <flag>_enabled`` reader:
    fails to ``default`` on a missing/broken config, a missing ``[teatree]``
    table, a missing key, or a non-boolean value, and returns the configured
    value only when it is a bare TOML boolean. So only a bare boolean ``false``
    disables a ``default=True`` flag and only a bare boolean ``true`` enables a
    ``default=False`` one — a QUOTED ``"false"`` / ``"true"`` (a string, not a
    bool) is ignored and the default stands. An explicit bare boolean is the
    one-line kill-switch / opt-in, never a code edit (NEVER-LOCKOUT).
    """
    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return default
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return default
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return default
    value = teatree.get(name)
    return value if isinstance(value, bool) else default
