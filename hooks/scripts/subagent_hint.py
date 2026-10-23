"""Keep owner-only escape instructions out of a sub-agent refusal."""

import re
import sys

from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent

sys.modules.setdefault("subagent_hint", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.subagent_hint", sys.modules[__name__])

_ESCAPE_HINT_RE = re.compile(r"`?\[gate-escape:[a-z0-9_.-]+:\s*<reason>\]`?")
_SUBAGENT_ESCALATE_HINT = "A sub-agent cannot grant an owner-only exception; ask the main agent to request approval."


def suppress_self_auth_hint_for_subagent(reason: str, data: dict, gate_id: str | None = None) -> str:
    try:
        if not gate_id or not call_is_from_subagent(data):
            return reason
        with teatree_src_on_path():
            from teatree.hooks.gate_escape import (  # noqa: PLC0415 — source path is optional until this hook runs
                EscapePolicy,
                policy_for,
            )

            if policy_for(gate_id) is EscapePolicy.AUDITED:
                return reason
        if not _ESCAPE_HINT_RE.search(reason):
            return reason
        return _ESCAPE_HINT_RE.sub(_SUBAGENT_ESCALATE_HINT, reason)
    except Exception:  # noqa: BLE001 — hint rewriting must not swallow a refusal.
        return reason
