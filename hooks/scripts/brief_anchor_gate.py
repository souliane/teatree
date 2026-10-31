"""Lint a sub-agent brief for unanchored factual assertions at dispatch time (#4341).

An orchestrator cannot know in advance WHICH of its assertions is stale — that is
the whole failure mode. This gate does not need to know either: it only checks that
the brief either anchors its claims to a commit or tells the sub-agent it may
overrule them, and quotes the one-sentence remedy when it does neither.

An unanchored brief is refused through the shared ``_fail_open_or_deny``
chokepoint, so it inherits the self-rescue allowlist and the master
``danger_gate_fail_open`` switch like every other over-deny gate.

NEVER-LOCKOUT: the ``brief_anchor_gate_enabled`` kill-switch (``t3 <overlay> gate brief-anchor
disable``) turns it off entirely; a per-call ``[gate-escape:brief_anchor: <reason>]`` token
in the first 512 chars of the brief clears one dispatch (an empty reason does not);
and any internal error, an unimportable ``teatree`` and an unreadable settings store
all fail OPEN.

Cold-import safe: the module top imports stdlib only — ``teatree`` is reached
through a deferred import after the ``sys.path`` bootstrap.
"""

import sys

from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.orchestration_boundary_signals import DISPATCH_TOOLS

# Alias the bare and ``hooks.scripts.`` identities so a test patching a helper here
# and the live hook's own import operate on ONE module object.
sys.modules.setdefault("brief_anchor_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.brief_anchor_gate", sys.modules[__name__])


def _gate_enabled() -> bool:
    from hooks.scripts.teatree_settings import teatree_bool_setting  # noqa: PLC0415 — deferred: cold-hook import

    return teatree_bool_setting("brief_anchor_gate_enabled")


def _prompt_of(data: dict) -> str:
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    prompt = tool_input.get("prompt", "")
    return prompt if isinstance(prompt, str) else ""


def _warning_for(prompt: str) -> str | None:
    """The lint warning for *prompt*, or ``None`` when it is anchored or asserts nothing."""
    with teatree_src_on_path():
        from teatree.hooks import brief_anchor_scanner  # noqa: PLC0415 — deferred: cold-hook import

        verdict = brief_anchor_scanner.find_unanchored_assertions(prompt)
        return None if verdict is None else brief_anchor_scanner.format_warning(verdict)


def handle_brief_anchor_lint(data: dict) -> bool:
    """Deny a dispatch whose brief anchors none of its claims."""
    try:
        if data.get("tool_name") not in DISPATCH_TOOLS or not _gate_enabled():
            return False
        prompt = _prompt_of(data)
        if not prompt:
            return False
        message = _warning_for(prompt)
        if message is None:
            return False
    except Exception:  # noqa: BLE001 — crash-proof hook: a gate bug must never wedge the dispatch
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 — deferred back-import: avoids a cycle

    return _fail_open_or_deny(data, message, gate_id="brief_anchor")
