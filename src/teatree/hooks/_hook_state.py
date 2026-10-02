"""Shared hook-state path resolution + a once-per-session override NOTE.

Consolidates the hook subsystem's on-disk state under ONE resolver so state does
not scatter across three fallback roots when ``T3_DATA_DIR`` is unset: the
quote-blocklist and the quote-scanner ledger resolve under
:func:`hook_state_root`. The repo-visibility cache deliberately does NOT -- it
caches a fact about a REMOTE repo, so it belongs to the host rather than to this
checkout and resolves through :func:`shared_hook_state_root`. Also carries the once-per-session stderr
NOTE that surfaces an inherited-env leak-gate override (``QUOTE_OK=1`` /
``ALLOW_BANNED_TERM=1`` from ``os.environ``) so a standing disable is visible
rather than silent. Stdlib-only + lazy ``teatree.paths`` import, so it stays
importable from the cold PreToolUse subprocess.
"""

import os
import sys
from pathlib import Path

# Redeclared (not imported): the module-boundary graph forbids
# ``teatree.hooks`` importing ``teatree.core``. Kept in sync with
# ``teatree.core.session_identity.SESSION_ID_ENV_VARS`` — a test pins the
# two identical (#3554).
_SESSION_ID_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "T3_LOOP_SESSION_ID",
)


def _current_session_key() -> str:
    """The session id under whichever name the harness exports it, ``""`` when none."""
    for name in _SESSION_ID_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


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


def note_env_override_once(override_name: str) -> None:
    """Emit a one-line stderr NOTE the first time an env-sourced override is honoured this session.

    ``QUOTE_OK=1`` / ``ALLOW_BANNED_TERM=1`` honoured from ``os.environ`` (a stray
    ``export`` or a Docker-composed env) silently disables every publish leak scan
    for the whole session. A session-keyed marker under :func:`hook_state_root`
    makes the standing disable VISIBLE without spamming every subsequent gated
    call. With no session id (marker un-keyable) the NOTE is emitted every time
    — still visible, never silent; a marker write failure still emits the NOTE
    (the NOTE matters more than the dedup).
    """
    message = (
        f"NOTE: {override_name}=1 is set in the process environment (os.environ), not on this "
        f"command — it disables the publish leak scan for EVERY publish this session. "
        f"Unset it if that was unintended.\n"
    )
    session = _current_session_key()
    if not session:
        sys.stderr.write(message)
        return
    marker = hook_state_root() / f".env-override-noted-{override_name}-{session}"
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass
    sys.stderr.write(message)
