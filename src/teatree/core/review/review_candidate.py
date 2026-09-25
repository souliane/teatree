"""Predicate enforcing the 4 review-candidate skip-conditions at the CLI/scanner layer (#1321).

Five autonomous-session bugs in a row came from agent-side BINDING memory
failing to apply the same 4 conditions before dispatching ``t3:reviewer``:

1. Author is the current user (cannot review own work).
2. Current user already approved the MR (or appears in ``approvers``).
    Sibling condition: any non-system note authored by the current user
    exists (review already engaged).
3. MR state is ``merged`` or ``closed`` (review-team broadcast points at
    already-done work — ``:white_check_mark:`` the broadcast and skip).
4. The originating Slack broadcast already carries a reaction (any emoji) or a
    thread reply from a HUMAN outside the self-set (another engineer picked it
    up, #159) — a ``B…``-prefixed bot/app-integration reply (e.g. the GitLab
    integration posting MR status into the thread) never counts as a claim.

The predicate is shape-tolerant: it reads both GitLab (``author.username``,
``state="opened"``, ``notes``) and GitHub (``user.login``, ``state="open"``)
shapes, plus the heterogeneous ``approvers`` list (strings or
``{"username": ...}`` / ``{"login": ...}`` dicts).
"""

import logging
from collections.abc import Iterable
from typing import cast
from urllib.parse import urlparse

from teatree.config import cold_reader
from teatree.core.backend_protocols import CodeHostBackend
from teatree.types import RawAPIDict

_MERGED_STATES = ("merged",)
_CLOSED_STATES = ("closed",)

logger = logging.getLogger(__name__)

_SELF_FORGE_IDENTITIES_SETTING = "self_forge_identities"


def _self_identity_set(current_user: str, self_identities: Iterable[str]) -> set[str]:
    """Union of every identity that counts as the user (#1321).

    The user owns more than one identity (a gitlab username plus one or
    more github logins). The self-conditions (author / approver / note)
    must match ANY of them, not only the primary ``current_user`` — an MR
    authored under a secondary alias is still the user's own work and must
    not dispatch ``t3:reviewer``.
    """
    return {name for name in (current_user, *self_identities) if name}


def author_username(mr: RawAPIDict) -> str:
    """Best-effort author username across GitLab (``author.username``) and GitHub (``user.login``)."""
    for key, sub in (("author", "username"), ("user", "login")):
        node = mr.get(key)
        if isinstance(node, dict):
            value = cast("RawAPIDict", node).get(sub)
            if isinstance(value, str):
                return value
    return ""


def author_is_self(author: str, *, current_user: str, self_identities: Iterable[str] = ()) -> bool:
    """Return True iff *author* is one of the user's own forge identities (#1321).

    The single notion of "the user authored this" reused by every path that
    must treat the user's own MR differently from a colleague's — the
    review-candidate skip-conditions (``author_is_self`` reason) and the
    Slack reaction scanners, which must never react on the user's own
    review-request post. ``author`` is the MR/PR author username already
    extracted from the forge payload (GitLab ``author.username`` / GitHub
    ``user.login``). An empty *author* never matches: an unknown author is
    not provably self, so a fail-closed reaction caller treats the non-match
    as "skip the reaction" rather than "react".
    """
    if not author:
        return False
    return author in _self_identity_set(current_user, self_identities)


def _resolve_self_identities(mr_url: str, identities: Iterable[str]) -> set[str]:
    """Resolve owner aliases plus configured bot identities for *mr_url*'s host."""
    resolved = {identity for identity in identities if identity}
    try:
        forge_host = (urlparse(mr_url).hostname or "").casefold()
    except ValueError:
        return resolved
    if not forge_host:
        return resolved
    configured = cold_reader.mapping_setting(_SELF_FORGE_IDENTITIES_SETTING).get(forge_host)
    if isinstance(configured, list):
        resolved.update(identity.strip() for identity in configured if isinstance(identity, str) and identity.strip())
    return resolved


def _is_self_authored(
    mr_url: str,
    host: CodeHostBackend | None,
    identities: Iterable[str],
) -> bool | None:
    """Return proved owner, proved colleague, or unresolved authorship for *mr_url*."""
    if host is None:
        return None
    try:
        author = host.get_pr_author(pr_url=mr_url)
    except Exception as exc:  # noqa: BLE001 — an unreadable author is the explicit unresolved verdict.
        logger.warning("review authorship: author lookup failed for %s: %s", mr_url, exc)
        return None
    if not author:
        return None
    self_identities = _resolve_self_identities(mr_url, identities)
    return author_is_self(author, current_user="", self_identities=self_identities)


def is_self_authored(
    mr_url: str,
    host: CodeHostBackend | None,
    identities: Iterable[str],
) -> bool | None:
    """Public boundary for consumers outside the teatree package."""
    return _is_self_authored(mr_url, host, identities)


def _approver_usernames(mr: RawAPIDict) -> list[str]:
    raw = mr.get("approvers")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            entry_dict = cast("RawAPIDict", entry)
            for sub in ("username", "login", "name"):
                value = entry_dict.get(sub)
                if isinstance(value, str) and value:
                    names.append(value)
                    break
    return names


def _self_has_non_system_note(mr: RawAPIDict, identities: set[str]) -> bool:
    notes = mr.get("notes")
    if not isinstance(notes, list):
        return False
    for note in notes:
        if not isinstance(note, dict):
            continue
        note_dict = cast("RawAPIDict", note)
        if bool(note_dict.get("system")):
            continue
        author = note_dict.get("author")
        if isinstance(author, dict):
            author_dict = cast("RawAPIDict", author)
            username = author_dict.get("username") or author_dict.get("login")
            if isinstance(username, str) and username in identities:
                return True
    return False


def broadcast_claimed_by_other(message: RawAPIDict, *, self_ids: Iterable[str]) -> bool:
    """True when anyone outside *self_ids* reacted on, or replied under, the broadcast (#159).

    Any emoji counts, not only ``:eyes:`` — a colleague's reaction is the sign they took
    the review. ``self_ids`` carries the user's Slack id AND the bot's own id, so the
    factory's review-DONE reactions never read as a colleague's claim. A THIRD-PARTY
    bot's thread reply (e.g. a GitLab-integration status post, ``B…``-prefixed) is
    likewise never a claim — only a human outside ``self_ids`` counts. A ``reply_count``
    with no readable ``reply_users`` fails closed: an unattributable reply is a colleague's.
    """
    own = {sid for sid in self_ids if sid}
    return _reacted_by_other(message, own) or _replied_by_other(message, own)


def _reacted_by_other(message: RawAPIDict, own: set[str]) -> bool:
    reactions = message.get("reactions")
    if not isinstance(reactions, list):
        return False
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        users = cast("RawAPIDict", reaction).get("users")
        if isinstance(users, list) and any(isinstance(user, str) and user and user not in own for user in users):
            return True
    return False


def _is_slack_bot_id(user_id: str) -> bool:
    """True iff *user_id* is a Slack bot/app-integration id (``B…``), never a human.

    Slack's own id-prefix convention (``U…`` = user, ``B…`` = bot/app
    integration) is a different namespace from
    :func:`teatree.core.review.review_taken.is_bot_author` (GitLab/GitHub
    usernames + ``user_type``), so that helper cannot recognise a Slack id —
    this mirrors its role for the Slack side rather than reusing its body.
    """
    return user_id.startswith("B")


def _replied_by_other(message: RawAPIDict, own: set[str]) -> bool:
    reply_users = message.get("reply_users")
    if isinstance(reply_users, list):
        return any(
            isinstance(user, str) and user and user not in own and not _is_slack_bot_id(user) for user in reply_users
        )
    count = message.get("reply_count")
    return isinstance(count, int) and count > 0


def should_review_candidate_reasons(
    mr: RawAPIDict,
    *,
    current_user: str,
    self_identities: Iterable[str] = (),
    broadcast: RawAPIDict | None = None,
) -> list[str]:
    """Return the ordered list of skip reasons; empty list means the MR is a candidate.

    ``self_identities`` carries the user's full identity set (gitlab
    username + every github login). The self-conditions match ANY of them
    so an MR authored / approved / commented under a secondary alias is
    recognised as the user's own work (#1321). ``current_user`` alone is
    honoured when no aliases are supplied (legacy single-identity callers).
    """
    identities = _self_identity_set(current_user, self_identities)
    reasons: list[str] = []
    if identities and author_is_self(author_username(mr), current_user=current_user, self_identities=self_identities):
        reasons.append("author_is_self")
    approvers = _approver_usernames(mr)
    if identities and identities.intersection(approvers):
        reasons.append("already_approved_by_self")
    if identities and _self_has_non_system_note(mr, identities):
        reasons.append("has_self_note")
    state = mr.get("state")
    if isinstance(state, str):
        if state in _MERGED_STATES:
            reasons.append("state_merged")
        elif state in _CLOSED_STATES:
            reasons.append("state_closed")
    if broadcast is not None and broadcast_claimed_by_other(broadcast, self_ids=identities):
        reasons.append("broadcast_reacted_by_other")
    return reasons


def should_review_candidate(
    mr: RawAPIDict,
    *,
    current_user: str,
    self_identities: Iterable[str] = (),
    broadcast: RawAPIDict | None = None,
) -> bool:
    """Apply the 4 skip-conditions; True iff the MR is a review candidate.

    See module docstring for the canonical list. ``self_identities`` is the
    user's full identity set (see :func:`should_review_candidate_reasons`).
    ``broadcast`` is the originating Slack-broadcast message dict
    (``reactions`` list); pass ``None`` when no broadcast applies (e.g. the
    GitLab/GitHub discover path).
    """
    return not should_review_candidate_reasons(
        mr,
        current_user=current_user,
        self_identities=self_identities,
        broadcast=broadcast,
    )
