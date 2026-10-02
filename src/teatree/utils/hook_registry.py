"""Where the Django-free hook tier keeps its loop registries.

``loop-registry.json`` (the tick-owner singleton) and
``consolidation-registry.json`` (the per-agent consolidation slots) are both
written by ``hooks/scripts/hook_router.py`` — a process with no teatree import
at all — and read back from the Django tier. That makes the directory a
cross-tier contract: a Django reader that resolves it differently reads a file
nobody writes, and reports "no owner" / "no holders" rather than "cannot see
it" (souliane/teatree#3828, the #3499 shape).

This module is that one Django-side answer. It lives beside
:mod:`teatree.utils.singleton`, the other primitive the hook tier and the
Django tier must agree on, and is deliberately NOT
:func:`teatree.paths.resolve_data_dir`: the writer has no teatree import and
therefore no worktree auto-isolation, so a reader that isolated would look in
the wrong place.
"""

import os
from pathlib import Path


def loop_registry_dir() -> Path:
    """The directory the hook tier writes its loop registries into.

    Mirrors ``hook_router._loop_registry_path`` precedence for precedence:
    ``T3_LOOP_REGISTRY_DIR`` -> ``$XDG_DATA_HOME/teatree`` ->
    ``~/.local/share/teatree``.
    """
    override = os.environ.get("T3_LOOP_REGISTRY_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree"


__all__ = ["loop_registry_dir"]
