"""Predicate enforcing the 4 review-candidate skip-conditions at the CLI/scanner layer (#1321).

Five autonomous-session bugs in a row came from agent-side BINDING memory
failing to apply the same 4 conditions before dispatching ``t3:reviewer``:

1. Author is the current user (cannot review own work).
2. Current user already approved the MR (or appears in ``approvers``).
    Sibling condition: any non-system note authored by the current user
    exists (review already engaged).
3. MR state is ``merged`` or ``closed`` (review-team broadcast points at
    already-done work — ``:white_check_mark:`` the broadcast and skip).
4. The originating Slack broadcast already carries a non-self reaction
    (another engineer picked it up).

The predicate is shape-tolerant: it reads both GitLab (``author.username``,
``state="opened"``, ``notes``) and GitHub (``user.login``, ``state="open"``)
shapes, plus the heterogeneous ``approvers`` list (strings or
``{"username": ...}`` / ``{"login": ...}`` dicts).
"""

from collections.abc import Iterable
from typing import cast

from teatree.types import RawAPIDict

_MERGED_STATES = ("merged",)
_CLOSED_STATES = ("closed",)


def _self_identity_set(current_user: str, self_identities: Iterable[str]) -> set[str]:
    """Union of every identity that counts as the user (#1321).

    The user owns more than one identity (a gitlab username plus one or
    more github logins). The self-conditions (author / approver / note)
    must match ANY of them, not only the primary ``current_user`` — an MR
    authored under a secondary alias is still the user's own work and must
    not dispatch ``t3:reviewer``.
    """
    return {name for name in (current_user, *self_identities) if name}


def _author_username(mr: RawAPIDict) -> str:
    """Best-effort author username across GitLab (``author.username``) and GitHub (``user.login``)."""
    for key, sub in (("author", "username"), ("user", "login")):
        node = mr.get(key)
        if isinstance(node, dict):
            value = cast("RawAPIDict", node).get(sub)
            if isinstance(value, str):
                return value
    return ""


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


def _broadcast_reacted_by_other(broadcast: RawAPIDict, current_user: str) -> bool:
    reactions = broadcast.get("reactions")
    if not isinstance(reactions, list):
        return False
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        users = cast("RawAPIDict", reaction).get("users")
        if not isinstance(users, list):
            continue
        for user in users:
            if isinstance(user, str) and user and user != current_user:
                return True
    return False


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
    if identities and _author_username(mr) in identities:
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
    if broadcast is not None and _broadcast_reacted_by_other(broadcast, current_user):
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
