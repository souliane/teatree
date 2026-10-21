"""The review nag's re-ask mention, resolved from the per-overlay ``review_nag_reask_mention`` setting.

The setting names a user group by id or handle, or a user by handle. A group
renders as a group mention and a user as a user mention, so a user id never
lands inside a group mention. An empty or unresolved fallback holds a re-ask
unless an engaged reviewer resolves safely.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from hashlib import sha256

from django.core.cache import cache as django_cache

from teatree.backends.slack.web_reads import is_slack_user_id
from teatree.config import get_effective_settings
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.core.models import ReviewRequestPost
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)
NAG_IDENTITY_LOOKUP_ERRORS = (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class RequesterIdentity:
    slack_id: str
    aliases: tuple[str, ...]


def engaged_reask_mention(
    messaging: MessagingBackend,
    host: CodeHostBackend | None,
    posts: Sequence[ReviewRequestPost],
    *,
    requester: RequesterIdentity,
    overlay_name: str,
) -> str:
    post = posts[0]
    fallback = reask_mention(messaging, overlay_name=overlay_name)
    if fallback == f"<@{requester.slack_id}>" and requester.slack_id:
        fallback = ""
    if not requester.slack_id:
        return fallback if fallback.startswith("<!subteam^") else ""
    if host is None:
        return fallback
    try:
        replies = messaging.fetch_thread_replies(channel=post.slack_channel_id, thread_ts=post.slack_thread_ts)
        slack_ids = _thread_engagers(replies, requester.slack_id)
        forge_names: set[str] = set()
        for request in posts:
            ref = pr_ref_from_url(request.mr_url)
            if ref is None:
                continue
            comments = host.list_pr_comments(repo=ref.slug, pr_iid=ref.pr_id)
            reviewers = host.get_pr_engaged_reviewers(repo=ref.slug, pr_iid=ref.pr_id)
            forge_names.update(_forge_engagers(comments, reviewers))
        for name in forge_names - set(requester.aliases):
            if user_id := _active_user_id(messaging, name, overlay_name=overlay_name, fallback=fallback):
                slack_ids.add(user_id)
    except NAG_IDENTITY_LOOKUP_ERRORS as exc:
        logger.warning("review_nag: engaged-reviewer lookup failed for %s: %s", post.mr_url, exc)
        return fallback
    slack_ids.discard(requester.slack_id)
    return " ".join(f"<@{user_id}>" for user_id in sorted(slack_ids)) or fallback


def _active_user_id(messaging: MessagingBackend, name: str, *, overlay_name: str, fallback: str) -> str:
    cache_key = "review-nag-unmapped:" + sha256(f"{overlay_name}\0{name.casefold()}".encode()).hexdigest()
    if not fallback and django_cache.get(cache_key):
        return ""
    user_id = messaging.resolve_unique_active_user_id(name)
    if not user_id and not fallback:
        django_cache.set(cache_key, 1, timeout=1800)
    return user_id


def _forge_engagers(comments: object, reviewers: object) -> set[str]:
    names: set[str] = set()
    if isinstance(reviewers, list):
        names.update(name for name in reviewers if isinstance(name, str))
    if not isinstance(comments, list):
        return names
    for comment in comments:
        if not isinstance(comment, dict) or comment.get("system") is True:
            continue
        author = comment.get("author") or comment.get("user")
        if not isinstance(author, dict):
            continue
        name = author.get("username") or author.get("login")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _thread_engagers(replies: object, requester_slack_id: str) -> set[str]:
    ids: set[str] = set()
    if not isinstance(replies, list):
        return ids
    for reply in replies:
        if not isinstance(reply, dict) or reply.get("bot_id") or reply.get("subtype") == "bot_message":
            continue
        user_id = reply.get("user")
        if is_slack_user_id(user_id):
            ids.add(user_id)
    ids.discard(requester_slack_id)
    return ids


def reask_mention(messaging: MessagingBackend, *, overlay_name: str) -> str:
    configured = get_effective_settings(overlay_name or None).review_nag_reask_mention
    if not configured:
        return ""
    try:
        if group_id := messaging.resolve_usergroup_id(configured):
            return f"<!subteam^{group_id}>"
        if user_id := messaging.resolve_user_id(configured):
            return f"<@{user_id}>"
    except Exception:
        logger.debug("review_nag: re-ask mention lookup failed for %r", configured, exc_info=True)
    _warn_unresolved_mention_once(overlay_name, configured)
    return ""


@cache
def _warn_unresolved_mention_once(overlay_name: str, configured: str) -> None:
    logger.warning(
        "review_nag: re-ask mention %r (overlay %r) resolves to no user group or user — holding the re-ask",
        configured,
        overlay_name,
    )


def reset_unresolved_mention_warnings() -> None:
    _warn_unresolved_mention_once.cache_clear()
