"""The sweep skill's ``orphan-worktree`` disposal guidance must be true and self-consistent (#4579).

Three defects shipped together in one paragraph: it claimed ``worktree teardown`` had nothing
to tear down for an unregistered checkout (``resolve_worktree`` auto-registers one, so it does),
it instructed ``git worktree remove`` — which the same file forbids three times over and the
``sweeping-worktrees`` eval negatively asserts — and the destructive-step section carried no arm
for the new kind at all, leaving a consumer with no sanctioned route.

The mention guard is scoped to the SENTENCE (or table row / bullet) carrying the reference, not
the paragraph: the defective paragraph did contain the word "never", just governing a different
clause, so a paragraph-wide predicate would have read green on the very text it exists to catch.
"""

import re
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1] / "skills" / "sweeping-worktrees" / "SKILL.md"
_HAND_ROLLED = "git worktree remove"
_PROHIBITION = re.compile(r"FORBIDDEN|STOP|by hand|hand-roll|[Nn]ever")
_SENTENCE_BREAK = re.compile(r"(?<=\.)\s+")


def _paragraphs(text: str) -> list[str]:
    return [block for block in text.split("\n\n") if block.strip()]


def _units(paragraph: str) -> list[str]:
    """A table row or list item is one unit; prose splits into sentences.

    Rows and bullets carry their verdict in a second column or after an arrow, so splitting
    them on the sentence break would strand the reference from the prohibition governing it.
    """
    if paragraph.lstrip().startswith(("|", "-", "*")):
        return paragraph.splitlines()
    return _SENTENCE_BREAK.split(paragraph.replace("\n", " "))


def _section(text: str, heading: str) -> str:
    body = text.split(heading, 1)[1]
    return body.split("\n### ", 1)[0]


@pytest.fixture(scope="module")
def skill() -> str:
    return _SKILL.read_text(encoding="utf-8")


def test_no_sentence_offers_a_hand_rolled_worktree_removal(skill: str) -> None:
    offers = [
        unit
        for paragraph in _paragraphs(skill)
        for unit in _units(paragraph)
        if _HAND_ROLLED in unit and not _PROHIBITION.search(unit)
    ]
    assert offers == [], f"the skill forbids this verb elsewhere; it must never instruct it: {offers}"


def test_the_orphan_record_paragraph_states_the_sanctioned_route(skill: str) -> None:
    matches = [block for block in _paragraphs(skill) if '`kind: "orphan-worktree"`' in block]
    assert len(matches) == 1, "the record-kind guidance must have exactly one home"
    paragraph = matches[0]

    assert "nothing to tear down" not in paragraph, (
        "`worktree teardown --path` reaches `_auto_register_from_git`, which creates the row"
    )
    assert "worktree teardown" in paragraph, "the kind's disposal must name the guarded verb"


def test_the_destructive_step_section_carries_an_arm_for_the_orphan_kind(skill: str) -> None:
    section = _section(skill, "### 3. Do the destructive step via the CLI")

    assert "orphan-worktree" in section, "the only section issuing a delete must cover every emitted kind"
    assert "worktree teardown --path" in section
