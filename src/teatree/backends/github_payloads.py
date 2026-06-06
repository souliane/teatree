"""GitHub payload shapes + pure parsers — the read-side of the gh backend.

Mirrors :mod:`teatree.backends.gitlab_payloads`: the TypedDict shapes teatree
reads off ``gh`` JSON and the pure functions that map a payload to a teatree
enum. No subprocess here — the I/O lives in :mod:`teatree.backends.github`.
"""

from typing import TypedDict, cast

from teatree.backends.protocols import PrOpenState, ReviewState

_GH_REVIEW_STATE_MAP: dict[str, ReviewState] = {
    "APPROVED": ReviewState.APPROVED,
    "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
    "DISMISSED": ReviewState.DISMISSED,
    "PENDING": ReviewState.PENDING,
}


class _GitHubUser(TypedDict, total=False):
    """Subset of the GitHub ``/user`` response that teatree reads."""

    login: str


class _GitHubReviewEntry(TypedDict, total=False):
    """Subset of the GitHub PR-review response that teatree reads."""

    user: _GitHubUser
    state: str


class _GitHubPullRequestSummary(TypedDict, total=False):
    """Subset of the GitHub PR response read for the review state lookup."""

    requested_reviewers: list[_GitHubUser]
    state: str
    merged: bool
    user: _GitHubUser


def latest_review_state_from_reviews(reviews: object, reviewer: str) -> ReviewState | None:
    """Return the most recent terminal review state by *reviewer*, or ``None``."""
    if not isinstance(reviews, list):
        return None
    for raw_entry in reversed(reviews):
        if not isinstance(raw_entry, dict):
            continue
        entry = cast("_GitHubReviewEntry", raw_entry)
        user = entry.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if login != reviewer:
            continue
        state_str = entry.get("state")
        if not isinstance(state_str, str):
            continue
        mapped = _GH_REVIEW_STATE_MAP.get(state_str.upper())
        if mapped is not None:
            return mapped
    return None


def pr_open_state_from_payload(pr: object) -> PrOpenState:
    """Map a GitHub PR payload to a :class:`PrOpenState` (#1074).

    ``state=="open"`` → OPEN; ``merged is True`` → MERGED; ``state=="closed"``
    without ``merged`` → CLOSED. Any non-dict or unrecognised shape →
    ``UNKNOWN`` so the orphan sweep fails open.
    """
    if not isinstance(pr, dict):
        return PrOpenState.UNKNOWN
    summary = cast("_GitHubPullRequestSummary", pr)
    if summary.get("state") == "open":
        return PrOpenState.OPEN
    if summary.get("merged") is True:
        return PrOpenState.MERGED
    if summary.get("state") == "closed":
        return PrOpenState.CLOSED
    return PrOpenState.UNKNOWN


def reviewer_is_requested(pr: object, reviewer: str) -> bool:
    """Return True iff *reviewer* appears on the PR's ``requested_reviewers``."""
    if not isinstance(pr, dict):
        return False
    requested = cast("_GitHubPullRequestSummary", pr).get("requested_reviewers")
    if not isinstance(requested, list):
        return False
    return any(isinstance(entry, dict) and entry.get("login") == reviewer for entry in requested)
