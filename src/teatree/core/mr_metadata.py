"""Deterministic MR title/description convention checks (#1540, #1367, #312).

Pure, overlay-agnostic helpers used by ``OverlayMetadata.validate_pr`` to
reject a non-conforming MR before the ``pr create`` network call, plus the
generator-side scaffold that emits the standard description body by default.

The validation checks each name the EXACT expected format so the caller can
fix the metadata without guessing: the title matches the overlay's effective
``mr_title_regex``; the description's FIRST LINE matches that same regex; the
description carries at least one What/Why header; and (when the overlay
declares them) every required section is present.

The first-line check mirrors the GitLab ``validate_mr_title_and_description``
CI gate, which parses the literal first line of the description and does NOT
fall back to the title (#1367) — so a description opening with ``## Summary``
reds the pipeline while the title is fine. Encoding the gate's own rule here
rejects the bad first line client-side, eliminating the validator round-trip.

The required-section mechanism (#312) lets an overlay declare mandatory extra
sections beyond What/Why — e.g. a ``Configuration`` section so every MR states
how to configure / enable / disable the change. The canonical wording lives in
the declaring overlay's own skill (core stays overlay-agnostic). The generator
(:func:`ensure_standard_body`) emits those sections by default; the gate
(:func:`validate_mr_metadata`) flags any that are missing.
"""

import re

from teatree.types import DEFAULT_MR_TITLE_REGEX

__all__ = [
    "DEFAULT_MR_TITLE_REGEX",
    "STANDARD_SECTIONS",
    "ensure_standard_body",
    "expected_first_line_format",
    "expected_title_format",
    "missing_required_sections",
    "validate_mr_metadata",
]

_WHAT_WHY_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(what|why)\b\s*:?", re.IGNORECASE | re.MULTILINE)

# The default body scaffold every generated description carries — overlay-
# agnostic. Overlays declare additional mandatory sections via
# ``OverlayMetadata.get_required_description_sections()`` (e.g. a
# ``Configuration`` section); those are appended to this base by
# ``ensure_standard_body``.
STANDARD_SECTIONS: tuple[str, ...] = ("What", "Why")


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


def _section_header_re(section: str) -> re.Pattern[str]:
    """A markdown header line for ``section`` (``# Header`` … ``###### Header``).

    Anchored to a line start and the header markers so a bare prose mention of
    the word (``the configuration is unchanged``) does NOT satisfy the section
    — only an actual ``## Configuration`` header counts.
    """
    return re.compile(rf"^\s*#{{1,6}}\s*{re.escape(section)}\b", re.IGNORECASE | re.MULTILINE)


def missing_required_sections(description: str, required_sections: list[str]) -> list[str]:
    """Return the declared sections absent from ``description`` (order preserved).

    Each required section is matched case-insensitively against a markdown
    header (``## <Section>``) anywhere in the body. The result is the subset of
    ``required_sections`` whose header is missing — empty means all present.
    """
    return [section for section in required_sections if not _section_header_re(section).search(description)]


def validate_mr_metadata(
    title: str,
    description: str,
    title_regex: str,
    *,
    required_sections: list[str] | None = None,
) -> list[str]:
    """Return convention violations for ``title``/``description`` (empty == valid).

    The title and the description's first line are each matched against
    ``title_regex`` (the overlay's effective ``mr_title_regex``); the
    description must be non-empty and carry a What or Why header (``## What`` /
    ``Why:`` and case/level variants). When ``required_sections`` is given,
    every declared section (e.g. ``Configuration``) must appear as a markdown
    header or the MR is flagged — this is the #312 enforcement the template
    alone does not give.
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
    errors.extend(
        f"MR description is missing a required '## {section}' section — "
        f"add a '## {section}' header documenting it (never leave it implicit)."
        for section in missing_required_sections(description, list(required_sections or []))
    )
    return errors


def ensure_standard_body(
    description: str,
    *,
    required_sections: list[str] | None = None,
    section_defaults: dict[str, str] | None = None,
) -> str:
    """Append any missing standard (``What``/``Why``) or required section.

    The generator builds ``description`` from the title + commit body; this
    helper guarantees it carries the standard scaffold plus every overlay-
    declared required section, so a thin commit still ships a description with
    every mandatory header. An already-present section is never duplicated, and
    the first line (the release-notes title) is preserved untouched.

    A missing section gets its ``## Header`` plus, when the overlay supplies one
    in ``section_defaults`` (case-insensitive key), that section's default body
    text — e.g. ``Configuration`` ships with the overlay's no-config line so the
    reviewer sees a meaningful default rather than an empty header. Core stays
    overlay-agnostic: it hard-codes no section wording, only renders what the
    overlay declares.
    """
    wanted = [*STANDARD_SECTIONS, *(required_sections or [])]
    missing = missing_required_sections(description, wanted)
    if not missing:
        return description
    defaults = {key.casefold(): value for key, value in (section_defaults or {}).items()}
    body = description.rstrip()
    additions = "\n\n".join(_render_section(section, defaults.get(section.casefold())) for section in missing)
    return f"{body}\n\n{additions}"


def _render_section(section: str, default_body: str | None) -> str:
    return f"## {section}\n{default_body}" if default_body else f"## {section}"
