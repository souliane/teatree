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

    FAIL OPEN — a Stop hook must be crash-proof: an absent ``t3`` binary, a
    non-zero exit, unparsable output, or any subprocess error resolves to
    ``False`` (do NOT suppress), so the availability / ownership gates still
    decide and the pump can never crash the session on an unreadable control
    plane.
    """
    status = _dispatch_loop_status()
    # An empty status means "unreadable" — fail OPEN (do not suppress).
    return bool(status) and status != _RUNNABLE_STATUS


def _dispatch_loop_status() -> str:
    """Durable status of the ``dispatch`` loop via ``t3``; ``""`` when unreadable.

    Reads ``t3 loop loop-state dispatch --json`` in a child process so the
    bare-``python3`` hook never needs ``django.setup()``. Any failure — absent
    ``t3``, non-zero exit, unparsable / non-dict output — yields ``""`` so the
    caller fails OPEN.
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return ""
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
