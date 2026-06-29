"""Stop self-pump gate for the durable DB ``LoopState`` 'pause everything' (#1913).

Extracted from :mod:`hook_router` (it is over the module-health LOC cap and may
only shrink): the in-session Stop self-pump must honour a durable DB pause of
the core ``dispatch`` loop. This DB ``LoopState`` tier is the SINGLE control
plane the self-pump consults (loop control is ``/loops`` + the DB only; there is
no env kill-switch). Keeping it in a sibling helper means the Stop hook gains the
behaviour without growing the god-module.

The read is **stdlib-only** (#2559). The harness invokes the Stop hook as a bare
``python3`` that has NO ``uv`` env — teatree's dependencies (Django et al.) are
not importable, so ``django.setup()`` cannot run in the hook interpreter. A read
gated on an in-process ``django.setup()`` therefore failed OPEN (never suppress)
under the real Stop hook, silently neutering ``t3 loop pause`` / migration 0087.
This module instead subprocesses the ``t3`` CLI — the editable install carries
its own venv, so it bootstraps Django in a CHILD process — exactly the way
``hook_router._consolidated_pending_work`` already reads ``pending-spawn``.
"""

import json
import shutil
import subprocess  # noqa: S404 — reads a trusted local ``t3`` binary, fixed argv, never shell

# The core loop the in-session Stop self-pump exists to drive. The
# self-pump re-fires ``t3 loop tick`` + ``claim-next``, which run this loop's
# dispatch fan-out; a durable DB hold on it IS the restart-surviving
# 'pause everything' (#1913). Mirrors :data:`teatree.loops.dispatch.loop.MINI_LOOP`'s name.
_DISPATCH_LOOP_NAME = "dispatch"

# A short bound — the Stop hook is timeout-capped (8s in hooks.json) and a
# read-only ``loop loop-state`` query is sub-second. Mirrors
# ``hook_router._SELF_PUMP_PENDING_TIMEOUT``.
_LOOP_STATE_READ_TIMEOUT = 5

# The single durable-runnable status (mirrors ``LoopStatus.ENABLED.value``
# without importing teatree). Any other resolved status — ``paused`` /
# ``disabled`` — is a durable hold that suppresses the self-pump.
_RUNNABLE_STATUS = "enabled"


def db_loop_state_suppresses_self_pump() -> bool:
    """True when the durable DB ``LoopState`` pauses/disables the loop (#1913).

    The DB-backed control tier is the SINGLE control plane: a ``PAUSED`` /
    ``DISABLED`` row on the core ``dispatch`` loop (the loop the self-pump
    drives) means the operator has durably paused the control plane, so the
    in-session Stop self-pump must suppress. Loop control is ``/loops`` + the DB
    only; there is no env kill-switch.

    The read is stdlib-only (#2559): it shells out to ``t3 loop loop-state
    dispatch --json`` (a read-only probe on the ``loop`` top-level group — no
    overlay token needed), so the durable status resolves in a CHILD ``t3``
    process that carries its own venv. The bare-``python3`` Stop hook
    interpreter never needs a ``django.setup()`` of its own.

    FAIL CLOSED on a reachable-but-unreadable control plane (pause must win,
    matching ``_pause_suppresses_self_pump``): when ``t3`` IS on PATH but the
    read fails (non-zero exit, unparsable, subprocess error), the durable status
    is INDETERMINATE — ``_dispatch_loop_status`` returns ``""`` and that
    suppresses, so a transiently-unreadable control plane cannot nag the loop
    through a possible pause. The ONE carve-out is fail OPEN: an absent ``t3``
    binary (``_dispatch_loop_status`` returns ``None``) means the loop genuinely
    cannot run ``t3`` at all — a loop that can't run ``t3`` can't be paused — so
    the availability / ownership gates decide instead.
    """
    status = _dispatch_loop_status()
    if status is None:
        # Binary absent — the loop can't run t3, so it can't be paused: fail OPEN.
        return False
    # Reachable: an enabled status pumps; any other (incl. an unreadable "")
    # suppresses. Reachable-but-unreadable fails CLOSED (pause must win).
    return status != _RUNNABLE_STATUS


def _dispatch_loop_status() -> str | None:
    """Durable status of the ``dispatch`` loop via ``t3``; split unreadable signals.

    Reads ``t3 loop loop-state dispatch --json`` in a child process so the
    bare-``python3`` hook never needs ``django.setup()``. Returns ``None`` ONLY
    when the ``t3`` binary is absent from PATH (the loop cannot run ``t3`` at all
    → the caller fails OPEN). A PRESENT binary whose read fails — non-zero exit,
    unparsable / non-dict output, subprocess error — yields ``""`` (reachable but
    unreadable → the caller fails CLOSED / suppresses).
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — trusted local binary, fixed argv, no shell
            [t3_bin, "loop", "loop-state", _DISPATCH_LOOP_NAME, "--json"],
            capture_output=True,
            text=True,
            timeout=_LOOP_STATE_READ_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("status", "")).strip().lower()


__all__ = ["db_loop_state_suppresses_self_pump"]
