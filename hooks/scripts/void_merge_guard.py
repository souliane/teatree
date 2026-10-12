"""Deny an agent's Bash call that would void a recorded merge; ``--dry-run`` stays open.

The detection lives in the ``teatree.hooks.void_merge_detect`` leaf, registered in the
shared hard-deny registry so Lane B refuses the same command. A narrow targeted-command
gate: it refuses one command shape, never arbitrary Bash. Cold-import safe: stdlib plus
the ``managed_repo`` sibling at module top.
"""

import sys

from hooks.scripts.managed_repo import teatree_src_on_path

sys.modules.setdefault("void_merge_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.void_merge_guard", sys.modules[__name__])


def handle_block_void_merge(data: dict) -> bool:
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 — deferred back-import

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    try:
        with teatree_src_on_path():
            from teatree.hooks import void_merge_detect  # noqa: PLC0415 — deferred: cold-hook import

            reason = void_merge_detect.void_merge_deny_reason(command)
    except Exception:  # noqa: BLE001 — crash-proof hook: the command-level operator checks still refuse
        return False
    if reason is None:
        return False
    return emit_pretooluse_deny(reason, gate_id="void_merge")
