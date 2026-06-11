"""Section-scoped system-prompt extraction — the eval token-cost lever.

The metered ``sdk`` lane drives one in-process Agent-SDK query per scenario and resends the
WHOLE ``agent_path`` SKILL.md as ``--system-prompt`` every time, with no
cross-scenario cache. The dominant input-token cost of a suite run is therefore
the sum of those whole-file prompts: ~1.6 M input tokens across the catalog,
half of it ``skills/rules/SKILL.md`` (77 KB) resent for 40 scenarios that each
test ONE of its ~50 rules.

A scenario that pins one rule does not need the other forty-nine in its system
prompt. When a spec declares ``agent_sections`` this module sends only those
``## `` sections (verbatim) of the SKILL.md plus the file's pre-first-heading
preamble (the framing title/intro). This is faithful — the section IS the rule
under test — and cuts that scenario's system-prompt input by the ratio of the
section size to the whole file.

A named section that does not exist RAISES (:class:`MissingSectionError`) rather
than silently contributing nothing: a typo'd anchor that sent an empty rule
prompt would make the scenario VACUOUS (the agent graded against framing text
with the rule removed), which is the exact failure the eval suite exists to catch.
Fail loud at load/build time instead.
"""

import re

# A markdown section header: ``## Title`` at the start of a line. The catalog's
# SKILL.md files use ``## `` for every rule/section heading (``# `` is the single
# file title). Match level-2+ headings so a section is delimited by the next
# heading of the same-or-shallower depth.
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*$", re.MULTILINE)


class MissingSectionError(ValueError):
    """A requested section name was not found in the agent definition."""


def extract_sections(text: str, section_names: tuple[str, ...]) -> str:
    """Return the file preamble plus the named ``## `` sections, in file order.

    ``section_names`` are matched against the heading TEXT (anchored on the ``## ``
    heading line), not as a free substring, so ``"Questions"`` never accidentally
    pulls a heading that merely contains the word. The preamble (everything before
    the first heading) is kept so the section retains its framing title/intro.

    A name with no matching heading raises :class:`MissingSectionError` — never a
    silent empty contribution (that would make the consuming scenario vacuous).
    """
    headings = list(_HEADING_RE.finditer(text))
    spans = _section_spans(text, headings)
    preamble = text[: headings[0].start()] if headings else text
    wanted = set(section_names)
    found: set[str] = set()
    chunks: list[str] = []
    for title, start, end in spans:
        if title in wanted:
            chunks.append(text[start:end].rstrip())
            found.add(title)
    missing = wanted - found
    if missing:
        ordered = [name for name in section_names if name in missing]
        msg = f"agent_sections not found in definition: {', '.join(ordered)}"
        raise MissingSectionError(msg)
    return (preamble.rstrip() + "\n\n" + "\n\n".join(chunks) + "\n").lstrip("\n")


def _section_spans(text: str, headings: list[re.Match[str]]) -> list[tuple[str, int, int]]:
    """``(heading_text, start, end)`` for each section, end = next heading start."""
    spans: list[tuple[str, int, int]] = []
    for index, match in enumerate(headings):
        title = match.group(2)
        start = match.start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        spans.append((title, start, end))
    return spans
