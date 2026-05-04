"""User-on-behalf posting identity.

Single source of truth for whether teatree appends an agent identity
(`Co-Authored-By`, "Sent using …", "Generated with …") to artifacts published
on the user's behalf — git commits, MR/PR comments, Slack messages, issue
bodies. Driven by the `[teatree] agent_signature` setting in
`~/.teatree.toml` (default `False`).

Every teatree code path that posts on the user's behalf must consult
`agent_signature_enabled()` (or append `agent_signature_suffix(text)` when
it would otherwise inject a signature). This keeps the policy in one place
and makes the rule trivially flippable per machine via config.

The companion rule for ad-hoc agent posting (MCP Slack, `gh` comments)
lives in `skills/rules/SKILL.md` § "No AI Signature on Posts Made on the
User's Behalf".
"""

from teatree.config import load_config


def agent_signature_enabled() -> bool:
    return load_config().user.agent_signature


def agent_signature_suffix(suffix: str) -> str:
    return suffix if agent_signature_enabled() else ""
