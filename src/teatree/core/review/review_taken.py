"""The forge-side sign that somebody else already took a review (#159).

The Slack-side sign lives in :mod:`teatree.core.review.review_candidate` (a colleague's
reaction or thread reply on the broadcast). This module reads the MR itself: a non-system
note from a human outside the self-set, or any approval, means a reviewer is already
engaged and the factory must not add a second one. Reads go through the resolved
:class:`~teatree.core.backend_protocols.CodeHostBackend`, so the deploy image needs no
``glab`` for it.
"""

import logging
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import cast

from teatree.core.backend_protocols import CodeHostBackend
from teatree.types import RawAPIDict
from teatree.utils.throttled_log import warn_throttled
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

#: GitLab project/group access tokens act as ``project_<id>_bot_<hash>`` users.
_ACCESS_TOKEN_BOT = re.compile(r"(?:^|_)bot(?:_|$)")


class ReviewTaken(StrEnum):
    TAKEN = "taken"
    FREE = "free"
    UNKNOWN = "unknown"


def is_bot_author(username: str, *, user_type: str = "") -> bool:
    return user_type.lower() == "bot" or username.endswith("[bot]") or bool(_ACCESS_TOKEN_BOT.search(username))


def _note_author(note: RawAPIDict) -> tuple[str, str]:
    """``(username, user_type)`` across GitLab ``author.username`` and GitHub ``user.login`` / ``user.type``."""
    for key, name_key in (("author", "username"), ("user", "login")):
        node = note.get(key)
        if isinstance(node, dict):
            node_dict = cast("RawAPIDict", node)
            name = node_dict.get(name_key)
            kind = node_dict.get("type")
            return (name if isinstance(name, str) else "", kind if isinstance(kind, str) else "")
    return ("", "")


def _human_note_by_other(note: RawAPIDict, own: set[str]) -> bool:
    if bool(note.get("system")):
        return False
    name, kind = _note_author(note)
    return bool(name) and name not in own and not is_bot_author(name, user_type=kind)


def review_taken_by_other(
    pr_url: str,
    *,
    self_identities: Iterable[str],
    host: CodeHostBackend | None,
) -> ReviewTaken:
    """Whether a human outside *self_identities* already reviews the PR at *pr_url*.

    ``TAKEN`` on any approval (the owner's own included — a human already reviewed)
    or on a non-system note by a human outside the self-set; ``FREE`` when neither;
    ``UNKNOWN`` when the URL, the host, or the forge read cannot answer — the caller
    defers the dispatch rather than risk a second review under the owner's identity.
    """
    ref = pr_ref_from_url(pr_url)
    if ref is None or host is None:
        return ReviewTaken.UNKNOWN
    try:
        approvals = host.get_mr_approvals(repo=ref.slug, pr_iid=ref.pr_id)
        notes = host.list_pr_comments(repo=ref.slug, pr_iid=ref.pr_id)
    except Exception as exc:  # noqa: BLE001 — an unread MR must never read as FREE; the caller defers, never duplicates.
        warn_throttled(
            logger,
            f"review-taken-probe:{pr_url}",
            "review-taken probe failed for %s — UNKNOWN: %s",
            pr_url,
            exc,
        )
        return ReviewTaken.UNKNOWN
    if approvals.get("approved_by"):
        return ReviewTaken.TAKEN
    own = {identity for identity in self_identities if identity}
    if any(isinstance(note, dict) and _human_note_by_other(note, own) for note in notes):
        return ReviewTaken.TAKEN
    return ReviewTaken.FREE


__all__ = ["ReviewTaken", "is_bot_author", "review_taken_by_other"]
