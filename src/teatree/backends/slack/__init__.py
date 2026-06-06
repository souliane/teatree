"""Slack integration backend (HTTP client, bot, reactions, tokens, voice).

Package facade re-exporting the cross-package public surface so callers import
from ``teatree.backends.slack`` while each symbol keeps an explicit defining
module (``client`` / ``bot`` / ``http`` …). ``mock.patch`` targets name the
defining submodule, never this facade.
"""

from teatree.backends.slack.client import (
    ReviewHistoryRead,
    SlackReviewMatch,
    SlackReviewSearchRequest,
    post_webhook_message,
    read_recent_review_matches,
    search_review_permalinks,
)

__all__ = [
    "ReviewHistoryRead",
    "SlackReviewMatch",
    "SlackReviewSearchRequest",
    "post_webhook_message",
    "read_recent_review_matches",
    "search_review_permalinks",
]
