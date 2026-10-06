"""The review nag's re-ask mention, resolved from the per-overlay ``review_nag_reask_mention`` setting.

The setting names a user group by id or handle, or a user by handle. A group
renders as a group mention and a user as a user mention, so a user id never
lands inside a group mention. Empty means no mention, and a value nothing
resolves is warned about once per process rather than on every tick.
"""

import logging
from functools import cache

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)


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
        "review_nag: re-ask mention %r (overlay %r) resolves to no user group or user — re-asking without a mention",
        configured,
        overlay_name,
    )


def reset_unresolved_mention_warnings() -> None:
    _warn_unresolved_mention_once.cache_clear()
