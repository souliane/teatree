"""Plan-before-code edit-gate escape scanner (PR-25 shrink).

The per-call ``[skip-plan-gate: <reason>]`` token scanner, factored out of the
shrink-only ``hook_router`` god-module so adding the plan-gate's transcript
marker (PR-25) nets the router SMALLER. ``hook_router.handle_block_edit_before_planned``
imports :func:`skip_plan_gate_token`; this module owns no I/O and no Django, so
it stays a fast pure-logic unit.
"""

import re
import sys

# Alias both identities so a bare ``from plan_edit_gate import ...`` (the live
# hook, whose dir is on sys.path) and ``hooks.scripts.plan_edit_gate`` (a
# subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("plan_edit_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.plan_edit_gate", sys.modules[__name__])

# Per-call escape for the plan-edit gate: ``[skip-plan-gate: <non-empty-reason>]``
# in the current Edit/Write tool call's new_string/content/file_path unblocks that
# single call. Mirrors ``_SKILL_LOAD_OK_RE`` / ``_SKIP_SKILL_GATE_RE`` in shape
# and 512-char truncation scope — buried tokens do not silently escape.
SKIP_PLAN_GATE_RE: re.Pattern[str] = re.compile(r"\[skip-plan-gate:\s*(\S[^\]]*?)\s*\]")

_TOKEN_SCAN_LIMIT = 512
_SKIP_TOKEN_FIELDS = ("new_string", "content", "file_path")


def skip_plan_gate_token(data: dict) -> str | None:
    """Return the reason from a ``[skip-plan-gate: <reason>]`` token, else None.

    Scans the current Edit/Write tool call's ``new_string``, ``content``, and
    ``file_path`` within the first 512 characters of each field — mirroring the
    skill-loading gate's token scanner — so a buried token in a long body does
    not silently authorise the call. An empty reason returns None.
    """
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    for field in _SKIP_TOKEN_FIELDS:
        value = tool_input.get(field, "")
        if not isinstance(value, str) or not value:
            continue
        match = SKIP_PLAN_GATE_RE.search(value[:_TOKEN_SCAN_LIMIT])
        if not match:
            continue
        reason = match.group(1).strip()
        if reason:
            return reason
    return None
