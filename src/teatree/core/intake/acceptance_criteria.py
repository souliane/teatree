"""Acceptance rows of an issue body, typed so a rubric can grade each by the right evidence."""

import re

from teatree.core.models.criterion_kind import CriterionKind, render_criterion

__all__ = ["ACCEPTANCE_HEADING", "CriterionKind", "acceptance_rows", "classify_row", "render_criterion"]

#: Both spellings of "behaviour" are written out in full rather than shortened to one
#: optional-letter alternation: that shape leaves a word fragment the codespell hook
#: "corrects", silently rewriting the pattern so it stops matching one of them.
ACCEPTANCE_HEADING = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]*\**[ \t]*"
    r"(?:acceptance|definition of done|success criteria|expected behaviour|expected behavior)\b",
    re.MULTILINE | re.IGNORECASE,
)

_ANY_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]", re.MULTILINE)
_BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?(?P<text>\S.*?)[ \t]*$")
_PROCESS_ROW = re.compile(
    r"\b(?:ci|lint|ruff|tach|prek|coverage|module-health|verify-gates|docs?|documentation|blueprint|"
    r"changelog|review(?:ed)?|approved|merged|squash(?:ed)?|commits?)\b",
    re.IGNORECASE,
)


def acceptance_rows(issue_body: str) -> list[str]:
    heading = ACCEPTANCE_HEADING.search(issue_body)
    if heading is None:
        return []
    after_heading = issue_body[heading.end() :].partition("\n")[2]
    next_heading = _ANY_HEADING.search(after_heading)
    section = after_heading[: next_heading.start()] if next_heading else after_heading
    rows: list[str] = []
    for line in section.splitlines():
        if bullet := _BULLET.match(line):
            rows.append(bullet["text"])
        elif line.strip() and rows:
            rows[-1] = f"{rows[-1]} {line.strip()}"
    return rows


def classify_row(row: str) -> CriterionKind:
    return CriterionKind.PROCESS if _PROCESS_ROW.search(row) else CriterionKind.BEHAVIOUR
