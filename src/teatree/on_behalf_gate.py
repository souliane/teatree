"""On-behalf posting pre-gate — setting resolver.

Single source of truth for the *binary* setting that decides whether
teatree must ask the user for explicit approval *before* publishing a
post made under the user's identity to a colleague/customer surface — a
PR/MR comment, an issue comment, a Slack channel/thread message, a
Notion post, a PR/MR approval, or a reaction on someone else's message.

The setting is ``[teatree] ask_before_post_on_behalf`` (default ``True``,
per-overlay overridable). This module is intentionally a thin layer
depending only on :mod:`teatree.config` — that lets the resolver be
imported from anywhere (including ``teatree.cli`` and ``teatree.core``)
without creating circular dependencies. The orchestration that actually
*satisfies* the gate (recorded-approval consume + audit) lives in
:mod:`teatree.core.on_behalf_gate_recorded`, which depends on this
module plus ``teatree.core.models``.

Every teatree code path that posts on the user's behalf calls
:func:`teatree.core.on_behalf_gate_recorded.require_on_behalf_approval`
*before* it publishes:

* gate **OFF** (the user has opted the overlay into trusted unattended
    posting) → ``require_on_behalf_approval`` returns, the post proceeds;
* gate **ON** + a recorded, unconsumed, exactly-scoped
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
    exists → it is consumed single-use, an
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfAudit` row is
    written, and the post proceeds;
* gate **ON** + no recorded approval → the helper raises
    :class:`~teatree.core.on_behalf_gate_recorded.OnBehalfPostBlockedError`.
    The caller MUST surface the blocked post to the user (the
    user-notify path) so it can be approved in plain text — never a
    silent drop, never an unattended post.

The user satisfies the gate **without a TTY** by recording an approval
(``t3 review approve-on-behalf <target> <action> --approver <id>``);
the agent then re-runs the post and the row is consumed. DMs *to the
user themselves* and internal-only orchestration writes (our own
backlog issues, durable memory, task bookkeeping, the sanctioned
``t3 ticket clear/merge`` keystone) are out of scope and remain
ungated.

The companion rule for ad-hoc agent posting (MCP Slack, raw ``gh`` /
``glab`` comments) lives in ``skills/rules/SKILL.md`` § "Ask Before
Posting on the User's Behalf".
"""

from teatree.config import get_effective_settings


def ask_before_post_on_behalf_enabled() -> bool:
    """Resolve the effective ``ask_before_post_on_behalf`` setting.

    Resolution follows the standard active-overlay → global → default
    chain (``get_effective_settings``), mirroring
    ``require_human_approval_to_answer`` — no env-var layer.
    """
    return get_effective_settings().ask_before_post_on_behalf
