"""Sub-agent deny-hint rewriting (#3252) — a bare sibling of the router split.

The banned-terms / quote-scanner leak denies advertise a false-positive escape
hatch as a leading override env prefix (``ALLOW_BANNED_TERM=1`` / ``QUOTE_OK=1``).
A SUB-AGENT cannot self-authorize that bypass: the auto-mode classifier denies the
retry as an "unauthorized safety-gate bypass", which poisoned the sub-agent's whole
context. :func:`suppress_self_auth_hint_for_subagent` rewrites the hint at the
router's ``emit_pretooluse_deny`` chokepoint when the call is from a sub-agent (a
non-empty ``agent_id``), pointing it at the route it CAN take — escalate to the
main agent / user — while the deny itself stays fail-closed. A main-agent deny
keeps the verbatim escape-hatch hint.

Extracted from ``hook_router`` (the #2384 router-shrink contract: a new concern
goes in a bare sibling, never the god-module) and imported back into the router.
Cold-import safe: stdlib only, no Django / ``teatree.core``.
"""

import re
import sys

from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent

# Alias both identities so a bare ``from subagent_hint import ...`` (live hook,
# whose dir is on sys.path) and the ``hooks.scripts.subagent_hint`` form
# (subprocess / test import) resolve the SAME module object.
sys.modules.setdefault("subagent_hint", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.subagent_hint", sys.modules[__name__])

# The self-authorize hint sentence the leak-gate deny messages emit: "If the match
# is a false positive, re-issue the command with a leading <ENV>=1 env prefix (e.g.
# `<ENV>=1 <command>`)." Matched as a unit so the whole sentence is replaced.
_SELF_AUTH_HINT_RE = re.compile(
    r"If the match is a false positive, re-issue the command with a leading "
    r"\w+=1 env prefix \(e\.g\. `[^`]*`\)\."
)
_SUBAGENT_ESCALATE_HINT = (
    "If the match is a false positive, do NOT retry with a self-bypass env prefix "
    "— a sub-agent cannot self-authorize it (the retry is denied as a safety-gate "
    "bypass); report the false positive to the main agent / user, who can authorize it."
)


def suppress_self_auth_hint_for_subagent(reason: str, data: dict) -> str:
    """Rewrite a self-authorize escape-hatch hint when the deny is for a sub-agent.

    Sub-agents (:func:`call_is_from_subagent`) cannot self-authorize the
    ``ALLOW_*=1`` / ``QUOTE_OK=1`` override, so the verbatim hint would only lead
    them into a classifier-denied retry loop (#3252). Main-agent denies keep the
    hint unchanged. Never raises — on any unexpected shape the reason is returned
    untouched (the deny must always be emitted).
    """
    try:
        if not call_is_from_subagent(data):
            return reason
        return _SELF_AUTH_HINT_RE.sub(_SUBAGENT_ESCALATE_HINT, reason)
    except Exception:  # noqa: BLE001 — a hint-rewrite fault must never drop the deny; emit the original.
        return reason
