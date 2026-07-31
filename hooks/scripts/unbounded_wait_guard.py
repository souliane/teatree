"""Deny a Bash command that starts an UNBOUNDED wait loop (#3882).

An agent blocks on CI, a commit, or a background job by writing
``until <condition>; do sleep N; done``. The loop is a child of the session's
shell: when the session ends — crash, token exhaustion, harness restart, user
stop — the loop is reparented and keeps polling, and its exit condition may never
become true because the thing it waited for was resolved by someone else or
abandoned. With no deadline that is permanent, and it spends a subprocess per tick
for as long as the box stays up.

This gate refuses to CREATE that shape. It is the prevention half on purpose:
detecting an already-running loop and killing it would mean deciding from the
outside that a live process is dead, which is the inference this repo's
conservative liveness rule (``core/worktree/checkout_liveness``,
``_workspace.owner_stamps.venue_can_observe``) exists to refuse — a wait whose
session id no longer resolves is not thereby a wait nobody is reading. A loop that
was never spawned unbounded needs no such judgement. Nothing here reads process
state, and nothing here signals a process.

The deny hands back the bounded rewrite (a ``timeout`` wrapper or a ``SECONDS``
deadline), so the agent gets its wait — one that ends on its own and says why.

The wait-shape detection lives in the ``teatree.hooks.unbounded_wait_detect`` leaf
(lazily imported inside the sibling ``src/`` bootstrap, #1314); this module is the
PreToolUse gate that drives it. The router re-exports
:func:`handle_block_unbounded_wait` into ``_HANDLERS`` unchanged.

Because the gate sits on the broad ``Bash`` matcher, its deny routes through the
router's shared ``_fail_open_or_deny`` chokepoint (back-imported lazily), so the
always-allowed self-rescue commands and the master ``danger_gate_fail_open``
kill-switch keep it from ever wedging a session (the never-lockout contract,
#2349). Fails OPEN on any import/internal error — a gate bug must never wedge the
agent.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with no
guarantee ``teatree`` is importable, so the module top imports only stdlib and the
already-extracted ``managed_repo`` sibling (the ``teatree_src_on_path``
bootstrap) — never Django / ``teatree.core``.
"""

import sys

from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("unbounded_wait_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.unbounded_wait_guard", sys.modules[__name__])


def handle_block_unbounded_wait(data: dict) -> bool:
    """Deny a Bash ``until``/``while … sleep`` wait that carries no deadline (#3882).

    A wait bounded by a ``timeout`` wrapper, the bash ``SECONDS`` builtin, or a
    ``date +%s`` epoch deadline passes through; so does a bare ``sleep``, a ``for``
    loop, and a ``while read`` loop, none of which can outlive their input. The deny
    reason carries the bounded rewrite so the agent re-issues the same wait with a
    deadline rather than being told only what it may not do.

    Routed through :func:`_fail_open_or_deny` so the always-allowed self-rescue
    commands and the master ``[teatree] danger_gate_fail_open`` kill-switch keep it
    from ever wedging a session (the never-lockout contract, #2349). Fails OPEN on
    any import/internal error. The handler bootstraps ``sys.path`` to import
    ``teatree`` from the sibling ``src/`` (#1314).
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    try:
        with _teatree_src_on_path():
            from teatree.hooks import unbounded_wait_detect  # noqa: PLC0415 — deferred: cold-hook import

            detection = unbounded_wait_detect.detect_unbounded_wait(command)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False
    if not detection.is_unbounded_wait:
        return False
    return _fail_open_or_deny(data, detection.message)
