"""Slack reactions on ticket state transitions.

When a ticket transitions between FSM states, we want the corresponding
Slack review-request message to get an emoji reaction so reviewers can see
the state change at a glance (``:tada:`` on merge, ``:arrows_counterclockwise:``
on rework, …).  The review permalink stored on each PR entry gives us the
Slack ``channel`` and ``timestamp`` needed for ``reactions.add``.

``add_reaction`` is the raw API call; :func:`add_reaction_verified` (#1192)
wraps it with an immediate verify-by-reread — a fresh ``reactions.get`` call
confirms the emoji is actually visible before the caller trusts the write.
The FSM-facing entry points below (``add_reactions_for_transition``,
``add_approval_reaction``) go through the verified wrapper.
"""

import json
import logging
import re
from typing import TYPE_CHECKING, cast

import httpx

from teatree.backends.slack.react_errors import SlackReactionError, build_react_error_message
from teatree.core.overlay_loader import get_overlay
from teatree.core.verify_by_reread import verify_by_reread

if TYPE_CHECKING:
    from teatree.core.models import PullRequest, Ticket
    from teatree.types import RawAPIDict

_APPROVAL_EMOJI = "white_check_mark"

# Engagement emojis signal "someone is looking at / picking up this PR" rather
# than "outcome reached". When the loop user is the ticket's author, posting
# one of these on the author's own review-team broadcast inverts the signal —
# colleagues read "the author is reviewing his own MR". Outcome emojis
# (``tada``, ``white_check_mark``, ``arrows_counterclockwise``) stay enabled
# for authored tickets because they communicate state regardless of who acts.
_ENGAGEMENT_EMOJIS: frozenset[str] = frozenset({"eyes", "hand", "raised_hand"})

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
    """Call Slack ``reactions.add``. Return True on success.

    Treats ``already_reacted`` as success — the desired end state is the
    emoji being present on the message. Transport-level failures (HTTP
    5xx, ``httpx.HTTPError``, and a 2xx body that is not parseable JSON)
    return ``False`` and are logged: a Slack outage or a proxy that
    returns HTML on a 2xx must not block FSM transitions, and there is no
    auth gap to surface.

    Slack-API-level failures (``ok:false`` — ``missing_scope``,
    ``not_in_channel``, ``mcp_externally_shared_channel_restricted``, …)
    raise :class:`SlackReactionError` (#1281). The pre-#1281 silent
    ``return False`` lets callers fall back to
    ``chat.postMessage(text=":emoji:")`` on the broadcast's thread —
    which the BINDING memory ``feedback_react_not_emoji_thread_comment``
    forbids. Raising loudly forecloses that substitute. FSM-side wrappers
    (:func:`add_reactions_for_transition`) catch the raise locally so a
    Slack auth gap during a state transition still degrades to a no-op
    that retries on the next tick.
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
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Slack reactions.add returned a non-JSON 2xx body: %s", exc)
        return False
    if payload.get("ok"):
        return True
    error = payload.get("error", "")
    if error == "already_reacted":
        return True
    raise SlackReactionError(error, build_react_error_message(error, channel_id, timestamp))


def _reaction_present(message: "RawAPIDict", emoji: str) -> bool:
    """True when *emoji* is present (any reactor, count > 0) in a ``reactions.get`` message block."""
    reactions = message.get("reactions")
    if not isinstance(reactions, list):
        return False
    for raw in reactions:
        if not isinstance(raw, dict):
            continue
        reaction = cast("RawAPIDict", raw)
        if reaction.get("name") != emoji:
            continue
        users = reaction.get("users")
        count = reaction.get("count")
        if (isinstance(users, list) and users) or (isinstance(count, int) and count > 0):
            return True
    return False


def _reread_reaction_present(token: str, channel_id: str, timestamp: str, emoji: str) -> bool:
    """Independent ``reactions.get`` read — never trust ``reactions.add``'s own response.

    Raises on a transport failure or a Slack ``ok:false`` so
    :func:`teatree.core.verify_by_reread.verify_by_reread` normalizes it to an
    ``not_confirmed`` outcome rather than a crash; the caller has already
    posted the reaction, so a broken reread must degrade, never raise.
    """
    response = httpx.get(
        "https://slack.com/api/reactions.get",
        headers={"Authorization": f"Bearer {token}"},
        params={"channel": channel_id, "timestamp": timestamp},
        timeout=10.0,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        msg = f"Slack reactions.get returned a non-JSON 2xx body for {channel_id}/{timestamp}: {exc}"
        raise RuntimeError(msg) from exc
    if not payload.get("ok"):
        msg = f"Slack reactions.get error={payload.get('error', '')!r} for {channel_id}/{timestamp}"
        raise RuntimeError(msg)
    message = payload.get("message") or {}
    return _reaction_present(message, emoji)


def add_reaction_verified(token: str, channel_id: str, timestamp: str, emoji: str) -> bool:
    """``add_reaction`` plus an immediate verify-by-reread that the emoji actually landed (#1192).

    ``add_reaction``'s own ``ok: true`` (or ``already_reacted``) response is
    necessary but not sufficient — Slack has been observed to accept a
    ``reactions.add`` call whose effect is not yet visible on a subsequent
    read. This wraps the raw post with an independent ``reactions.get``
    re-read via :func:`teatree.core.verify_by_reread.verify_by_reread` before
    trusting the write. Returns ``False`` both when the write itself failed
    and when the reread could not confirm it — either way the caller may
    safely retry on the next tick, since ``reactions.add`` is idempotent.
    """
    if not add_reaction(token, channel_id, timestamp, emoji):
        return False
    outcome = verify_by_reread(
        label=f"slack_reaction:{channel_id}:{timestamp}:{emoji}",
        reread=lambda: _reread_reaction_present(token, channel_id, timestamp, emoji),
    )
    if not outcome.confirmed:
        logger.warning(
            "Slack reactions.add(%s) on %s/%s not confirmed by reread: %s",
            emoji,
            channel_id,
            timestamp,
            outcome.reason,
        )
    return outcome.confirmed


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

    Engagement emojis (``eyes``, ``hand``, ``raised_hand``) are gated when
    ``ticket.role == "author"``: the loop user is the PR's author, so an
    "I'm engaging" reaction on the author's own review-team broadcast
    misrepresents the author as reviewing their own MR. Outcome emojis
    (``tada``, ``white_check_mark``, ``arrows_counterclockwise``) post
    regardless because they communicate state rather than engagement.
    """
    overlay = get_overlay(name=ticket.overlay or None)
    emoji = overlay.config.get_transition_emojis().get(transition_name)
    if not emoji:
        return 0

    if emoji in _ENGAGEMENT_EMOJIS and _ticket_role(ticket) == "author":
        logger.info(
            "Skipping %s reaction on authored ticket %s (transition=%s) — "
            "author cannot signal engagement on their own PR broadcast.",
            emoji,
            getattr(ticket, "pk", "?"),
            transition_name,
        )
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
        try:
            success = add_reaction_verified(token, channel_id, timestamp, emoji)
        except SlackReactionError as exc:
            # A Slack auth gap (missing_scope, restricted channel, …)
            # must surface to a human, but not roll back the FSM
            # transition — the next tick re-tries. Log + continue so
            # later permalinks still get their reaction attempted.
            logger.warning(
                "Slack reactions.add refused for %s/%s (emoji=%s): %s",
                channel_id,
                timestamp,
                emoji,
                exc,
            )
            continue
        if success:
            posted += 1
    return posted


def _ticket_role(ticket: "Ticket") -> str:
    """Return ``ticket.role`` as a plain string, defaulting to ``"author"``.

    ``Ticket.role`` is a CharField backed by :class:`Ticket.Role` text choices;
    callers in tests may pass a ``SimpleNamespace`` without the attribute, so
    fall back to the model default (``author``) the same way Django does.
    """
    return str(getattr(ticket, "role", "author") or "author")


def add_approval_reaction(pull_request: "PullRequest") -> int:
    """Post a ✅ on the requester's review-request Slack message (#961).

    Driven by ``PullRequest.approve()`` (the approve-on-behalf action).
    The review-request message is the one whose permalink was stored on
    the PR as ``slack_url`` at ``request_review`` time. Returns 1 on a
    successful reaction, 0 on any no-op (missing slack_url, unparsable
    permalink, missing token). Never raises — a Slack outage must not
    block the FSM transition.
    """
    permalink = pull_request.slack_url
    if not permalink:
        return 0
    parsed = parse_permalink(permalink)
    if not parsed:
        return 0

    overlay = get_overlay(name=pull_request.overlay or None)
    token = overlay.config.get_slack_token()
    if not token:
        return 0

    channel_id, timestamp = parsed
    try:
        success = add_reaction_verified(token, channel_id, timestamp, _APPROVAL_EMOJI)
    except SlackReactionError as exc:
        # Approval reaction is FSM-coupled: a Slack auth gap surfaces in
        # the log but must not roll back the approve() transition.
        logger.warning(
            "Slack reactions.add refused for %s/%s (emoji=%s): %s",
            channel_id,
            timestamp,
            _APPROVAL_EMOJI,
            exc,
        )
        return 0
    return 1 if success else 0


class SlackReactionPublisher:
    """Adapts the module reaction functions to ``core.reaction_dispatch.ReactionPublisher``.

    Registered into the core registry by ``BackendsConfig.ready()`` so
    ``core.signals`` reaches the Slack reaction publisher without importing
    ``backends`` (#1922).
    """

    def add_reactions_for_transition(self, ticket: "Ticket", transition_name: str) -> int:  # noqa: PLR6301
        return add_reactions_for_transition(ticket, transition_name)

    def add_approval_reaction(self, pull_request: "PullRequest") -> int:  # noqa: PLR6301
        return add_approval_reaction(pull_request)
