"""Deterministic MR title/description convention checks (#1540).

Pure, overlay-agnostic helpers used by ``OverlayMetadata.validate_pr`` to
reject a non-conforming MR before the ``pr create`` network call. The title
must match the overlay's effective ``mr_title_regex`` and the description must
be non-empty with at least one What/Why header. Both error strings name the
EXACT expected format so the caller can fix the metadata without guessing.
"""

import re

from teatree.types import DEFAULT_MR_TITLE_REGEX

__all__ = ["DEFAULT_MR_TITLE_REGEX", "expected_title_format", "validate_mr_metadata"]

_WHAT_WHY_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(what|why)\b\s*:?", re.IGNORECASE | re.MULTILINE)


def expected_title_format(title_regex: str) -> str:
    return f"MR title must match the overlay convention: {title_regex}"


def _has_what_why_header(description: str) -> bool:
    return _WHAT_WHY_RE.search(description) is not None


def validate_mr_metadata(title: str, description: str, title_regex: str) -> list[str]:
    """Return convention violations for ``title``/``description`` (empty == valid).

    The title is matched against ``title_regex`` (the overlay's effective
    ``mr_title_regex``). The description must be non-empty and carry a What or
    Why header (``## What`` / ``Why:`` and case/level variants).
    """
    errors: list[str] = []
    if not re.search(title_regex, title):
        errors.append(f"{expected_title_format(title_regex)} (got: {title!r})")
    if not description.strip():
        errors.append("MR description is empty — add a What/Why body (e.g. '## What' / '## Why').")
    elif not _has_what_why_header(description):
        errors.append("MR description must contain at least one What or Why header (e.g. '## What' / '## Why').")
    return errors
