"""Section-scoped system-prompt extraction — the eval token-cost lever.

The metered ``api`` lane drives one in-process Agent-SDK query per scenario and resends the
WHOLE ``agent_path`` SKILL.md as ``--system-prompt`` every time, with no
cross-scenario cache. The dominant input-token cost of a suite run is therefore
the sum of those whole-file prompts: 18.1 MB across the catalog were nothing
narrowed, two thirds of it ``skills/rules/SKILL.md`` (155 KB) resent for 78 specs
that each test ONE of its 73 sections. The specs narrowed so far take what is
actually sent down to 10.5 MB.

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
HEADING_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*$", re.MULTILINE)


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
    headings = list(HEADING_RE.finditer(text))
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
    """``(heading_text, start, end)`` per section, end at the next SAME-OR-SHALLOWER heading.

    Ending at the next heading of any depth would stop a section at its own first
    subsection, so a rule whose carve-outs and worked examples live under deeper
    headings would reach the grader stripped of them — a silently narrowed rule that
    every matcher still passes.
    """
    spans: list[tuple[str, int, int]] = []
    for index, match in enumerate(headings):
        depth = len(match.group(1))
        end = next(
            (later.start() for later in headings[index + 1 :] if len(later.group(1)) <= depth),
            len(text),
        )
        spans.append((match.group(2), match.start(), end))
    return spans
