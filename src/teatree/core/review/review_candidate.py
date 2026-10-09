"""Predicate enforcing the 4 review-candidate skip-conditions at the CLI/scanner layer (#1321).

Five autonomous-session bugs in a row came from agent-side BINDING memory
failing to apply the same 4 conditions before dispatching ``t3:reviewer``:

1. Author is the current user (cannot review own work).
2. Current user already approved the MR (or appears in ``approvers``).
    Sibling condition: any non-system note authored by the current user
    exists (review already engaged).
3. MR state is ``merged`` or ``closed`` (review-channel broadcast points at
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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.self_forge_identities import declared_identities_for_url
from teatree.types import RawAPIDict

_MERGED_STATES = ("merged",)
_CLOSED_STATES = ("closed",)

logger = logging.getLogger(__name__)


class AuthorshipVerdict(StrEnum):
    SELF = "self"
    FOREIGN = "foreign"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class AuthorshipAssessment:
    target: str
    author: str
    verdict: AuthorshipVerdict


class AuthorshipObserver(Protocol):
    def __call__(self, assessment: AuthorshipAssessment) -> None: ...


_AUTHORSHIP_OBSERVER: ContextVar[AuthorshipObserver | None] = ContextVar("authorship_observer", default=None)


@contextmanager
def observe_authorship(observer: AuthorshipObserver) -> Iterator[None]:
    token = _AUTHORSHIP_OBSERVER.set(observer)
    try:
        yield
    finally:
        _AUTHORSHIP_OBSERVER.reset(token)


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
    return {identity for identity in identities if identity} | set(declared_identities_for_url(mr_url))


def _is_self_authored(
    mr_url: str,
    host: CodeHostBackend | None,
    identities: Iterable[str],
) -> bool | None:
    """Return proved owner, proved colleague, or unresolved authorship for *mr_url*."""
    if host is None:
        return _observed_authorship(mr_url, "", AuthorshipVerdict.UNREADABLE)
    try:
        author = host.get_pr_author(pr_url=mr_url)
    except Exception as exc:  # noqa: BLE001 — an unreadable author is the explicit unresolved verdict.
        logger.warning("review authorship: author lookup failed for %s: %s", mr_url, exc)
        return _observed_authorship(mr_url, "", AuthorshipVerdict.UNREADABLE)
    if not author:
        return _observed_authorship(mr_url, "", AuthorshipVerdict.UNREADABLE)
    # Forge logins are case-insensitive, so a case-only difference is still the same account.
    self_identities = {name.casefold() for name in _resolve_self_identities(mr_url, identities)}
    verdict = (
        AuthorshipVerdict.SELF
        if author_is_self(author.casefold(), current_user="", self_identities=self_identities)
        else AuthorshipVerdict.FOREIGN
    )
    return _observed_authorship(mr_url, author, verdict)


def _observed_authorship(target: str, author: str, verdict: AuthorshipVerdict) -> bool | None:
    observer = _AUTHORSHIP_OBSERVER.get()
    if observer is not None:
        observer(AuthorshipAssessment(target=target, author=author, verdict=verdict))
    if verdict is AuthorshipVerdict.UNREADABLE:
        return None
    return verdict is AuthorshipVerdict.SELF


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
