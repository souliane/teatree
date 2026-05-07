"""Detect "approvals dismissed by push" events from MR system notes.

Sourced from system notes attached to MR discussions (already fetched). The
note bodies that GitLab emits when the ``reset_approvals_on_push`` setting
fires vary by version, so the dismissal pattern is intentionally permissive
("removed all approvals"). The approver attribution is reconstructed by
replaying ``approved`` / ``unapproved`` system notes in chronological order.

The signal is suppressed whenever ``current_approval_count > 0`` — a positive
current count means someone has re-approved after any dismissal, so consumers
would only nag on stale state.
"""

import operator
import re
from dataclasses import dataclass

from teatree.core.sync import RawAPIDict

_DISMISSED_BODY_RE = re.compile(r"^removed all approvals", re.IGNORECASE)
_APPROVED_BODY_RE = re.compile(r"^approved (this )?merge request", re.IGNORECASE)
_UNAPPROVED_BODY_RE = re.compile(r"^unapproved (this )?merge request", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ApprovalDismissal:
    at: str
    approvers: list[str]


def detect_approval_dismissal(
    discussions: list[RawAPIDict],
    *,
    current_approval_count: int,
) -> ApprovalDismissal | None:
    """Return the most recent push-triggered approval dismissal, or None.

    Returns None when ``current_approval_count > 0`` — a re-approval supersedes
    any earlier dismissal, so the signal would be stale.
    """
    if current_approval_count > 0:
        return None

    events = sorted(_iter_approval_events(discussions), key=operator.itemgetter(0))
    if not events:
        return None

    approvers: list[str] = []
    last_dismissal: ApprovalDismissal | None = None
    for created_at, kind, username in events:
        if kind == "approved":
            if username and username not in approvers:
                approvers.append(username)
        elif kind == "unapproved":
            if username in approvers:
                approvers.remove(username)
        elif kind == "dismissed":
            if approvers:
                last_dismissal = ApprovalDismissal(at=created_at, approvers=list(approvers))
            approvers = []
    return last_dismissal


def _iter_approval_events(discussions: list[RawAPIDict]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for disc in discussions:
        if not isinstance(disc, dict):
            continue
        notes = disc.get("notes", [])
        if not isinstance(notes, list):
            continue
        for note in notes:
            event = _note_as_event(note)
            if event is not None:
                out.append(event)
    return out


def _note_as_event(note: object) -> tuple[str, str, str] | None:
    if not isinstance(note, dict):
        return None
    if not note.get("system"):  # ty: ignore[invalid-argument-type]
        return None
    body = str(note.get("body", ""))  # ty: ignore[no-matching-overload]
    created_at = str(note.get("created_at", ""))  # ty: ignore[no-matching-overload]
    author = note.get("author", {})  # ty: ignore[no-matching-overload]
    username = str(author.get("username", "")) if isinstance(author, dict) else ""
    if _DISMISSED_BODY_RE.search(body):
        return (created_at, "dismissed", username)
    if _APPROVED_BODY_RE.search(body):
        return (created_at, "approved", username)
    if _UNAPPROVED_BODY_RE.search(body):
        return (created_at, "unapproved", username)
    return None
