"""On-behalf posting pre-gate.

Single source of truth for whether teatree must ask the user for explicit
approval *before* publishing a post made under the user's identity to a
colleague/customer surface — a PR/MR comment, an issue comment, a Slack
channel/thread message, a Notion post, a PR/MR approval, or a reaction on
someone else's message.

The "how / what / to-whom / when" of a colleague reply is not generic or
encodable, so the encodable mechanism is the gate itself: ask first. The
setting ``[teatree] ask_before_post_on_behalf`` (default ``True``, per-
overlay overridable) drives it. It is the *pre*-gate companion to the
notify-*after* path (#949): both ship on; the user flips this one off
per-overlay first, once confident the system posts well.

Every teatree code path that posts on the user's behalf consults
``ask_before_post_on_behalf_enabled()`` and refuses to publish unattended
while it is true. DMs *to the user themselves* and internal-only
orchestration writes (our own backlog issues, durable memory, task
bookkeeping, the sanctioned ``t3 ticket clear/merge``) are out of scope.

The companion rule for ad-hoc agent posting (MCP Slack, raw ``gh`` /
``glab`` comments) lives in ``skills/rules/SKILL.md`` § "Ask Before Posting
on the User's Behalf".
"""

from teatree.config import get_effective_settings


def ask_before_post_on_behalf_enabled() -> bool:
    """Resolve the effective ``ask_before_post_on_behalf`` setting.

    Resolution follows the standard active-overlay → global → default
    chain (``get_effective_settings``), mirroring
    ``require_human_approval_to_answer`` — no env-var layer.
    """
    return get_effective_settings().ask_before_post_on_behalf
