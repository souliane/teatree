"""Stdlib-only away-mode read for the bare-``python3`` Stop/PreToolUse hooks (#2559).

The harness invokes the hooks as a bare ``python3`` with NO ``uv`` env, so
teatree's dependencies are not importable and ``django.setup()`` cannot run in
the hook interpreter. ``_resolved_away_mode`` previously bootstrapped Django
in-process to call ``resolve_mode()``; under the real hook that bootstrap failed
and the lever returned ``False`` (never away) — silently neutering
``t3 <overlay> availability away`` as a self-pump suppressor.

This module reads the resolved availability mode WITHOUT ``django.setup()`` by
subprocessing the ``t3`` CLI (the editable install carries its own venv, so it
bootstraps Django in a CHILD process), parsing the ``mode=…`` token from the
``availability show`` line — exactly the stdlib subprocess shape
``hook_router._consolidated_pending_work`` and the sibling
``loop_state_self_pump_gate`` already use.

It lives in a bare sibling module (not ``hook_router``) so the over-cap,
shrink-only router gains the stdlib behaviour without growing (``hooks/CLAUDE.md``
§ "Adding a gate").
"""

import os
import shutil
import subprocess  # noqa: S404 — reads a trusted local ``t3`` binary, fixed argv, never shell

_AWAY_READ_TIMEOUT = 5

# The always-registered overlay's CLI alias — the fall-through token for the
# overlay-agnostic hooks. ``t3-teatree`` ships in core's pyproject, so
# ``t3 teatree …`` always resolves; ``resolve_mode()`` is overlay-independent, so
# ANY registered overlay's ``availability show`` returns the same mode.
_DEFAULT_OVERLAY_ALIAS = "teatree"


def resolved_away_mode() -> bool:
    """True when the resolved availability mode is ``away`` (stdlib read).

    Shells out to ``t3 <overlay> availability show`` and parses its
    ``mode=…`` token. The overlay token is ``T3_OVERLAY_NAME`` when set, falling
    back to the always-registered ``teatree`` alias; the choice never changes the
    answer because ``resolve_mode()`` is overlay-agnostic.

    FAIL SAFE for the caller: an absent ``t3``, a failed subprocess, or
    unparsable output resolves to ``False`` here. The self-pump's
    ``_pause_suppresses_self_pump`` then applies its own
    suppress-on-indeterminate rule on top (a clean ``present`` pumps; a raised
    read suppresses).
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return False
    for overlay in _overlay_candidates():
        out = _availability_show(t3_bin, overlay)
        if out is not None:
            return _line_reports_away(out)
    return False


def _overlay_candidates() -> list[str]:
    """Overlay CLI tokens to try for ``availability show``, in order.

    ``T3_OVERLAY_NAME`` first (when set), then the always-registered ``teatree``
    alias as the never-empty fall-through — de-duplicated, order preserved.
    """
    candidates: list[str] = []
    env_overlay = os.environ.get("T3_OVERLAY_NAME", "").strip()
    if env_overlay:
        candidates.append(env_overlay)
    if _DEFAULT_OVERLAY_ALIAS not in candidates:
        candidates.append(_DEFAULT_OVERLAY_ALIAS)
    return candidates


def _availability_show(t3_bin: str, overlay: str) -> str | None:
    """Stdout of ``t3 <overlay> availability show``; ``None`` on any failure."""
    try:
        result = subprocess.run(  # noqa: S603 — trusted local binary, fixed argv, no shell
            [t3_bin, overlay, "availability", "show"],
            capture_output=True,
            text=True,
            timeout=_AWAY_READ_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout


def _line_reports_away(text: str) -> bool:
    """True when an ``availability: mode=… source=…`` line resolves to ``away``."""
    for raw in text.splitlines():
        for token in raw.split():
            key, sep, value = token.partition("=")
            if sep and key.strip() == "mode":
                return value.strip().lower() == "away"
    return False


__all__ = ["resolved_away_mode"]
