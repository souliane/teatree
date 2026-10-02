"""Self-pin that every AGENTS.md First Principle renders as its own list item.

The section is written as a CommonMark ordered list, and CommonMark ordered-list
markers are digits only. An inserted principle numbered ``6a.`` therefore parses
as a lazy continuation of the previous item: it renders INSIDE principle 6's
``<li>``, is invisible as a numbered item, and cannot be cross-referenced — while
the source still reads as a numbered principle. Review does not catch that,
because the source looks correct.

The check is deliberately marker-level rather than a render through a markdown
library: the failure is a marker the parser will not accept, and asserting the
marker set keeps the check free of an undeclared transitive parser dependency.
"""

import re
from pathlib import Path

_AGENTS = Path(__file__).resolve().parents[2] / "AGENTS.md"

_SECTION_HEADING_RE = re.compile(r"^##\s+First Principles\b")
_NEXT_SECTION_RE = re.compile(r"^##\s")
#: A principle: a line-initial ordered-list marker followed by the bolded title.
_PRINCIPLE_RE = re.compile(r"^(\d+)\.\s+\*\*")
#: Anything line-initial that LOOKS like a numbered principle but carries a
#: marker CommonMark will not parse as an ordered-list item (``6a.``, ``6).``).
_MALFORMED_MARKER_RE = re.compile(r"^\d+[^.\s\d][^\s]*\.\s+\*\*")


def _section_lines() -> list[str]:
    lines: list[str] = []
    in_section = False
    for line in _AGENTS.read_text(encoding="utf-8").splitlines():
        if not in_section:
            if _SECTION_HEADING_RE.match(line):
                in_section = True
            continue
        if _NEXT_SECTION_RE.match(line):
            break
        lines.append(line)
    return lines


def test_the_first_principles_section_is_found() -> None:
    assert _section_lines(), "AGENTS.md has no '## First Principles' section to check"


def test_every_principle_carries_a_parsable_ordered_list_marker() -> None:
    malformed = [line for line in _section_lines() if _MALFORMED_MARKER_RE.match(line)]
    assert not malformed, (
        "A First Principle uses a marker CommonMark cannot parse as an ordered-list "
        "item, so it renders inside the previous principle instead of as its own: "
        f"{malformed}. Renumber it — markers are digits only."
    )


def test_the_principles_are_a_gapless_sequence_from_one() -> None:
    numbers = [int(m.group(1)) for line in _section_lines() if (m := _PRINCIPLE_RE.match(line))]
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"First Principles must be a gapless 1..N with no repeats (got {numbers})"
    )
