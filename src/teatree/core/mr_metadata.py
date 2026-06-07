"""Deterministic MR title/description convention checks (#1540, #1367).

Pure, overlay-agnostic helpers used by ``OverlayMetadata.validate_pr`` to
reject a non-conforming MR before the ``pr create`` network call. Three checks,
each naming the EXACT expected format so the caller can fix the metadata
without guessing: the title matches the overlay's effective ``mr_title_regex``;
the description's FIRST LINE matches that same regex; and the description
carries at least one What/Why header.

The first-line check mirrors the GitLab ``validate_mr_title_and_description``
CI gate, which parses the literal first line of the description and does NOT
fall back to the title (#1367) — so a description opening with ``## Summary``
reds the pipeline while the title is fine. Encoding the gate's own rule here
rejects the bad first line client-side, eliminating the validator round-trip.
"""

import re

from teatree.types import DEFAULT_MR_TITLE_REGEX

__all__ = [
    "DEFAULT_MR_TITLE_REGEX",
    "expected_first_line_format",
    "expected_title_format",
    "validate_mr_metadata",
]

_WHAT_WHY_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(what|why)\b\s*:?", re.IGNORECASE | re.MULTILINE)


def expected_title_format(title_regex: str) -> str:
    return f"MR title must match the overlay convention: {title_regex}"


def expected_first_line_format(title_regex: str) -> str:
    return (
        "MR description first line will fail the GitLab "
        "validate_mr_title_and_description CI gate — it must be the "
        f"conventional-commit form {title_regex} (prepend the MR title as line "
        "1, blank line, then the body)."
    )


def _has_what_why_header(description: str) -> bool:
    return _WHAT_WHY_RE.search(description) is not None


def _first_line(description: str) -> str:
    return description.split("\n", 1)[0]


def validate_mr_metadata(title: str, description: str, title_regex: str) -> list[str]:
    """Return convention violations for ``title``/``description`` (empty == valid).

    The title and the description's first line are each matched against
    ``title_regex`` (the overlay's effective ``mr_title_regex``); the
    description must be non-empty and carry a What or Why header (``## What`` /
    ``Why:`` and case/level variants).
    """
    errors: list[str] = []
    if not re.search(title_regex, title):
        errors.append(f"{expected_title_format(title_regex)} (got: {title!r})")
    if not description.strip():
        errors.append("MR description is empty — add a What/Why body (e.g. '## What' / '## Why').")
        return errors
    first = _first_line(description)
    if not re.search(title_regex, first):
        errors.append(f"{expected_first_line_format(title_regex)} (got: {first!r})")
    if not _has_what_why_header(description):
        errors.append("MR description must contain at least one What or Why header (e.g. '## What' / '## Why').")
    return errors
