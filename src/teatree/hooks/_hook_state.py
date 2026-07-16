"""Shared hook-state path resolution + a once-per-session override NOTE.

Consolidates the hook subsystem's on-disk state under ONE resolver so state does
not scatter across three fallback roots when ``T3_DATA_DIR`` is unset: the
quote-blocklist, the quote-scanner ledger, and the repo-visibility cache all
resolve under :func:`hook_state_root`. Also carries the once-per-session stderr
NOTE that surfaces an inherited-env leak-gate override (``QUOTE_OK=1`` /
``ALLOW_BANNED_TERM=1`` from ``os.environ``) so a standing disable is visible
rather than silent. Stdlib-only + lazy ``teatree.paths`` import, so it stays
importable from the cold PreToolUse subprocess.
"""

import os
import sys
from pathlib import Path


def hook_state_root() -> Path:
    """The single root for hook on-disk state.

    ``T3_DATA_DIR`` wins (the explicit override every gate already honours);
    otherwise the canonical XDG data dir (:data:`teatree.paths.DATA_DIR`), so the
    blocklist, ledger, and visibility cache converge instead of scattering across
    ``~/.teatree`` / ``~/.cache`` / the data dir.
    """
    base = os.environ.get("T3_DATA_DIR")
    if base:
        return Path(base)
    from teatree.paths import DATA_DIR  # noqa: PLC0415 — deferred: paths resolves the data dir at import

    return DATA_DIR


def note_env_override_once(override_name: str) -> None:
    """Emit a one-line stderr NOTE the first time an env-sourced override is honoured this session.

    ``QUOTE_OK=1`` / ``ALLOW_BANNED_TERM=1`` honoured from ``os.environ`` (a stray
    ``export`` or a Docker-composed env) silently disables every publish leak scan
    for the whole session. A session-keyed marker under :func:`hook_state_root`
    makes the standing disable VISIBLE without spamming every subsequent gated
    call. With no ``CLAUDE_SESSION_ID`` (marker un-keyable) the NOTE is emitted
    every time — still visible, never silent; a marker write failure still emits
    the NOTE (the NOTE matters more than the dedup).
    """
    message = (
        f"NOTE: {override_name}=1 is set in the process environment (os.environ), not on this "
        f"command — it disables the publish leak scan for EVERY publish this session. "
        f"Unset it if that was unintended.\n"
    )
    session = os.environ.get("CLAUDE_SESSION_ID", "").strip()
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
