"""User-on-behalf posting identity.

Single source of truth for whether teatree appends an agent identity
(`Co-Authored-By`, "Sent using …", "Generated with …") to artifacts published
on the user's behalf — git commits, PR comments, Slack messages, issue
bodies. Driven by the DB-home `agent_signature` setting (#1775): set it via
`t3 <overlay> config_setting set agent_signature true` (default `False`).

Every teatree code path that posts on the user's behalf must consult
`agent_signature_enabled()` (or append `agent_signature_suffix(text)` when
it would otherwise inject a signature). This keeps the policy in one place
and makes the rule trivially flippable per machine via config.

The companion rule for ad-hoc agent posting (MCP Slack, `gh` comments)
lives in `skills/rules/SKILL.md` § "No AI Signature on Posts Made on the
User's Behalf".
"""

from teatree.config import get_effective_settings


def agent_signature_enabled() -> bool:
    return get_effective_settings().agent_signature


def agent_signature_suffix(suffix: str) -> str:
    return suffix if agent_signature_enabled() else ""
