"""Sub-agent deny-hint rewriting (#3252) — a bare sibling of the router split.

A quote-scanner deny offers the owner a per-call ``[quote-ok: <reason>]`` approval.
A sub-agent must not see that token in its refusal. :func:`suppress_self_auth_hint_for_subagent`
rewrites the hint at the router's ``emit_pretooluse_deny`` chokepoint when the call is from a
sub-agent (a non-empty ``agent_id``), pointing it at the route it CAN take — escalate to the
main agent / user — while the deny itself stays fail-closed. Public egress ignores override
tokens and requires owner escalation.

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

# Remove the owner-approval hint — the ``[<gate>-ok: <reason>]`` token, where it goes, and the
# note on where it is NOT read — from sub-agent refusals while retaining the owner's
# rephrase-or-escalate path. Sub-agents must not retry with a bypass token.
_SELF_AUTH_HINT_RE = re.compile(
    r"The owner may approve this one \w+ with a per-call `\[[a-z][a-z-]*-ok: <reason>\]` override[^.]*\."
    r"(?: A token placed [^.]*\.)?"
)
_SUBAGENT_ESCALATE_HINT = (
    "A sub-agent cannot self-authorize an override; report the false match to the main agent / user."
)


def suppress_self_auth_hint_for_subagent(reason: str, data: dict) -> str:
    """Rewrite a self-authorize escape-hatch hint when the deny is for a sub-agent.

    Sub-agents (:func:`call_is_from_subagent`) cannot self-authorize the
    ``[quote-ok: <reason>]`` approval, so naming it could lead them into a
    classifier-denied retry loop (#3252). Never raises — on any unexpected shape
    the reason is returned untouched (the deny must always be emitted).
    """
    try:
        if not call_is_from_subagent(data):
            return reason
        return _SELF_AUTH_HINT_RE.sub(_SUBAGENT_ESCALATE_HINT, reason)
    except Exception:  # noqa: BLE001 — a hint-rewrite fault must never drop the deny; emit the original.
        return reason
