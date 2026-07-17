"""Shared extraction of forge-issue fields from a raw backend issue dict.

The single source of truth for reading an issue's human title off the raw
API payload, so the issue-implementer scanner (which surfaces it in a claim
signal) and the loop persistence layer (which stamps it onto the ticket for
the dashboard) can never drift on how a title is read.
"""

from teatree.types import RawAPIDict


def issue_title(issue: RawAPIDict) -> str:
    """The issue's ``title``, or ``""`` when absent or not a string."""
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def issue_title_from_payload(payload: RawAPIDict) -> str:
    """The issue title carried on a dispatch payload's ``raw`` issue dict.

    A ``issue_implementer.claimed`` / ``assigned_issue.ready`` payload carries
    the full backend issue under ``raw``; this reads its title, degrading to
    ``""`` when the payload has no usable raw issue.
    """
    raw = payload.get("raw")
    return issue_title(raw) if isinstance(raw, dict) else ""
