"""Deny a Bash command that signals a process by a raw, guessed pid (#2384 PR5).

The agent has twice killed the WRONG, LIVE process by guessing which ``claude``
pid 'looked dead'. A bare ``kill <pid>`` / ``kill -9 <pid>`` at a command
position is exactly that guessed-pid shape; this gate denies it so the agent must
go through the runnable ``t3 teatree safe-kill <pid> --hang-cause`` command
(positive session/task id + non-live proof) instead. ``kill -0`` (the no-op
liveness probe), ``pkill`` / ``killall`` (signal by name), ``%job`` / ``$VAR`` /
``$(…)`` targets, and a ``kill`` token inside a comment / string / as another
command's argument are NOT flagged.

The raw-pid shape detection lives in the ``teatree.hooks.safe_kill_detect`` leaf
(lazily imported inside the sibling ``src/`` bootstrap, #1314); this module is the
PreToolUse gate that drives it. Extracted whole from ``hook_router`` (the #2384
Wave-2 router split, PR5) so the dispatcher shrinks; the router re-exports
:func:`handle_block_raw_pid_kill` into ``_HANDLERS`` unchanged.

Because the gate sits on the broad ``Bash`` matcher, its deny routes through the
router's shared ``_fail_open_or_deny`` chokepoint (back-imported lazily), so the
always-allowed self-rescue commands and the master ``danger_gate_fail_open``
kill-switch keep it from ever wedging a session (the never-lockout contract,
#2349); the ``emit_pretooluse_deny`` / ``_write_pretooluse_deny`` deny writer
stays in the router. Fails OPEN on any import/internal error — a gate bug must
never wedge the agent.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
the already-extracted ``managed_repo`` sibling (the ``teatree_src_on_path``
bootstrap) — never Django / ``teatree.core``. The ``_fail_open_or_deny`` spine
helper stays in the router and is back-imported lazily inside the handler body.
"""

import sys

from managed_repo import teatree_src_on_path as _teatree_src_on_path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("raw_pid_kill_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.raw_pid_kill_guard", sys.modules[__name__])


def handle_block_raw_pid_kill(data: dict) -> bool:
    """Deny a Bash command that signals a process by a raw, guessed pid (#2225).

    The agent has twice killed the WRONG, LIVE process by guessing which
    ``claude`` pid 'looked dead'. A bare ``kill <pid>`` / ``kill -9 <pid>`` at a
    command position is exactly that guessed-pid shape; it is denied so the agent
    must go through the runnable ``t3 teatree safe-kill <pid> --hang-cause``
    command (positive session/task id + non-live proof) instead. ``kill -0``
    (the no-op liveness probe), ``pkill``/``killall`` (signal by name),
    ``%job``/``$VAR``/``$(…)`` targets, and a ``kill`` token inside a comment /
    string / as another command's argument are NOT flagged.

    Because the gate sits on the broad ``Bash`` matcher, its deny is routed
    through :func:`_fail_open_or_deny` so the always-allowed self-rescue commands
    and the master ``[teatree] danger_gate_fail_open`` kill-switch keep it from
    ever wedging a session (the never-lockout contract, #2349). Fails OPEN on any
    import/internal error — a gate bug must never wedge the agent. The handler
    bootstraps ``sys.path`` to import ``teatree`` from the sibling ``src/`` (#1314).
    """
    from hook_router import _fail_open_or_deny  # noqa: PLC0415, PLC2701

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    try:
        with _teatree_src_on_path():
            from teatree.hooks import safe_kill_detect  # noqa: PLC0415

            detection = safe_kill_detect.detect_raw_pid_kill(command)
    except Exception:  # noqa: BLE001
        return False
    if not detection.is_raw_pid_kill:
        return False
    return _fail_open_or_deny(data, detection.message)
