"""Slack reactions on ticket state transitions.

When a ticket transitions between FSM states, we want the corresponding
Slack review-request message to get an emoji reaction so reviewers can see
the state change at a glance (``:tada:`` on merge, ``:arrows_counterclockwise:``
on rework, …).  The review permalink stored on each PR entry gives us the
Slack ``channel`` and ``timestamp`` needed for ``reactions.add``.
"""

import logging
import re
from typing import TYPE_CHECKING

import httpx

from teatree.core.overlay_loader import get_overlay

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)

_PERMALINK_RE = re.compile(r"/archives/(?P<channel>[^/]+)/p(?P<ts>\d+)")
_SLACK_TS_FRACTIONAL_DIGITS = 6


def parse_permalink(permalink: str) -> tuple[str, str] | None:
    """Extract ``(channel_id, timestamp)`` from a Slack archive permalink.

    Permalinks look like ``https://team.slack.com/archives/C0123/p1700000000000100``.
    The timestamp format expected by the Slack API reinserts the dot 6 digits
    from the right: ``1700000000.000100``.
    """
    if not permalink:
        return None
    match = _PERMALINK_RE.search(permalink)
    if not match:
        return None
    channel = match.group("channel")
    raw_ts = match.group("ts")
    if len(raw_ts) <= _SLACK_TS_FRACTIONAL_DIGITS:
        return None
    split_at = -_SLACK_TS_FRACTIONAL_DIGITS
    ts = f"{raw_ts[:split_at]}.{raw_ts[split_at:]}"
    return channel, ts


def add_reaction(token: str, channel_id: str, timestamp: str, emoji: str) -> bool:
    """Call Slack ``reactions.add``. Return True on success, False otherwise.

    Treats ``already_reacted`` as success — the desired end state is the
    emoji being present on the message.  All failures are logged but never
    raised; Slack outages must not block FSM transitions.
    """
    if not (token and channel_id and timestamp and emoji):
        return False
    try:
        response = httpx.post(
            "https://slack.com/api/reactions.add",
            headers={"Authorization": f"Bearer {token}"},
            data={"channel": channel_id, "timestamp": timestamp, "name": emoji},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Slack reactions.add failed: %s", exc)
        return False
    if not response.is_success:
        logger.warning("Slack reactions.add HTTP %s", response.status_code)
        return False
    payload = response.json()
    if payload.get("ok"):
        return True
    error = payload.get("error", "")
    if error == "already_reacted":
        return True
    logger.warning("Slack reactions.add error: %s", error)
    return False


def _iter_pr_permalinks(ticket: "Ticket") -> list[str]:
    """Collect non-empty ``review_permalink`` values from the ticket's PRs."""
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    prs = extra.get("prs", {})
    if not isinstance(prs, dict):
        return []
    permalinks: list[str] = []
    for pr in prs.values():
        if not isinstance(pr, dict):
            continue
        permalink = pr.get("review_permalink")
        if isinstance(permalink, str) and permalink:
            permalinks.append(permalink)
    return permalinks


def add_reactions_for_transition(ticket: "Ticket", transition_name: str) -> int:
    """Add the emoji mapped to *transition_name* to every PR permalink.

    Returns the number of successful reaction posts.  Missing credentials,
    missing permalinks, and unmapped transitions are all silent no-ops.
    """
    overlay = get_overlay()
    emoji = overlay.config.get_transition_emojis().get(transition_name)
    if not emoji:
        return 0

    token = overlay.config.get_slack_token()
    if not token:
        return 0

    posted = 0
    for permalink in _iter_pr_permalinks(ticket):
        parsed = parse_permalink(permalink)
        if not parsed:
            continue
        channel_id, timestamp = parsed
        if add_reaction(token, channel_id, timestamp, emoji):
            posted += 1
    return posted
