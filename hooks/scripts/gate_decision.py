"""Hook deny envelope and bounded gate identity at the telemetry boundary."""

import contextlib
import json
import sys

from hooks.scripts.deny_circuit_breaker import _deny_gate_id
from hooks.scripts.gate_ledger import write_gate_decision


def record_gate_decision(reason: str, *, decision: str, gate_id: str | None, context: tuple[str, dict]) -> None:
    """Append ledger evidence for a PreToolUse decision; a ledger failure never changes the decision."""
    event, data = context
    if event != "PreToolUse":
        return
    with contextlib.suppress(Exception):
        session_id = data.get("session_id") if isinstance(data, dict) else None
        rule = _deny_gate_id(reason) if ":" in reason else "unknown"
        write_gate_decision(
            gate=gate_id or rule,
            decision=decision,
            rule=rule,
            session_id=session_id if isinstance(session_id, str) else "",
        )


def write_pretooluse_deny(reason: str, *, gate_id: str | None, context: tuple[str, dict]) -> bool:
    """Write the legacy and modern Claude deny envelopes, then local evidence."""
    payload = {
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    if gate_id:
        payload["gate_id"] = gate_id
        payload["hookSpecificOutput"]["gate_id"] = gate_id
    json.dump(payload, sys.stdout)
    record_gate_decision(reason, decision="deny", gate_id=gate_id, context=context)
    return True
