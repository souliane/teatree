"""Shared hook-state path resolution.

Consolidates the hook subsystem's on-disk state under ONE resolver so state does
not scatter across three fallback roots when ``T3_DATA_DIR`` is unset: the
quote-blocklist and the quote-scanner ledger resolve under
:func:`hook_state_root`. The repo-visibility cache deliberately does NOT -- it
caches a fact about a REMOTE repo, so it belongs to the host rather than to this
checkout and resolves through :func:`shared_hook_state_root`. Stdlib-only + lazy ``teatree.paths`` import, so it stays
importable from the cold PreToolUse subprocess.
"""

import os
from pathlib import Path


def hook_state_root() -> Path:
    """The single root for hook on-disk state.

    ``T3_DATA_DIR`` wins (the explicit override every gate already honours);
    otherwise the canonical XDG data dir (:data:`teatree.paths.DATA_DIR`), so the
    blocklist and ledger converge instead of scattering across ``~/.teatree`` /
    ``~/.cache`` / the data dir. Per-worktree isolation is inherited from
    :data:`~teatree.paths.DATA_DIR` and is correct here: this state describes the
    checkout that wrote it.
    """
    from teatree.paths import data_dir_root  # noqa: PLC0415 — deferred: paths resolves the data dir at import

    return data_dir_root()


def shared_hook_state_root() -> Path:
    """:func:`hook_state_root` WITHOUT the per-worktree auto-isolation.

    For hook state that describes the OUTSIDE world rather than this checkout -- the
    repo-visibility cache, whose entries are facts about a REMOTE repo. Isolating those
    per worktree gave N worktrees N independently-frozen answers to one question, so a
    single remote URL resolved PRIVATE in one worktree and UNKNOWN in the next.
    ``T3_DATA_DIR`` still wins, so a test or sandbox redirects this exactly as it
    redirects :func:`hook_state_root`.
    """
    from teatree.paths import (  # noqa: PLC0415 — deferred: paths resolves the data dir at import
        PRIMARY_CLONE_SENTINEL,
        resolve_data_dir,
    )

    override = os.environ.get("T3_DATA_DIR")
    if override:
        return Path(override)
    return resolve_data_dir(env=dict(os.environ), home=Path.home(), repo_root=PRIMARY_CLONE_SENTINEL).path
