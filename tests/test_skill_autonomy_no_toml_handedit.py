"""souliane/teatree#1737: skills route the autonomy knob through the CLI.

The trust switch must be set via ``t3 <overlay> autonomy set`` — never by
instructing the agent to assign the underlying keys directly.

``t3 <overlay> autonomy set`` (souliane/teatree#1729) is the first-class
surface for the single per-overlay ``autonomy`` knob, which collapses the
three approval gates including ``require_human_approval_to_answer``. A skill
that still tells the agent to set those keys directly contradicts the
anti-hand-edit doctrine and the CLI. This guard scans the live skill tree
(not a diff) so the prohibition holds for every skill file.

Two hand-edit shapes are flagged, each only when the paragraph carries no
``t3 … autonomy`` CLI route. A setting verb applied to an autonomy-key
assignment (``set `autonomy = "full"```) or a table-qualified assignment
(``[overlays.<name>].require_human_approval_to_answer = false``). Descriptive
tier references that merely name a value (``the `autonomy = "full"` tier``)
are not instructions and are not flagged.
"""

import re
from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

_AUTONOMY_KEYS = r"(?:autonomy|require_human_approval_to_answer)"

_CLI_ROUTE = re.compile(rf"t3 [^`]*{_AUTONOMY_KEYS}\s+(?:set|show)")
_SET_VERB_ASSIGNMENT = re.compile(rf"\b(?:set|flip|flips|write|edit)\b[^.`]*?`?{_AUTONOMY_KEYS}\s*=")
_TABLE_PATH_ASSIGNMENT = re.compile(rf"(?:\]\.|\[(?:teatree|overlays)[^]]*\][^`]*?){_AUTONOMY_KEYS}\s*=")


def _iter_skill_files() -> list[Path]:
    return sorted([*_SKILLS_DIR.glob("*/SKILL.md"), *_SKILLS_DIR.glob("*/references/*.md")])


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """Group hard-wrapped lines into paragraphs (blank-line separated).

    Markdown prose is hard-wrapped, so a setting verb and its ``key = value``
    can land on adjacent physical lines. Evaluating the joined paragraph
    keeps the guard robust to wrapping. Returns ``(first_line_number, text)``.
    """
    grouped: list[tuple[int, str]] = []
    start = 0
    buffer: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not buffer:
                start = lineno
            buffer.append(line.strip())
        elif buffer:
            grouped.append((start, " ".join(buffer)))
            buffer = []
    if buffer:
        grouped.append((start, " ".join(buffer)))
    return grouped


def _handedit_lines(text: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for lineno, paragraph in _paragraphs(text):
        if _CLI_ROUTE.search(paragraph):
            continue
        if _SET_VERB_ASSIGNMENT.search(paragraph) or _TABLE_PATH_ASSIGNMENT.search(paragraph):
            findings.append((lineno, paragraph))
    return findings


class TestHandeditDetector:
    def test_flags_set_verb_assignment(self) -> None:
        prose = 'set `autonomy = "full"` / `"notify"` (preferred) for the overlay'
        assert _handedit_lines(prose) == [(1, prose.strip())]

    def test_flags_answer_approval_flip(self) -> None:
        prose = "the user flips per-overlay (`require_human_approval_to_answer = false`)"
        assert len(_handedit_lines(prose)) == 1

    def test_flags_toml_table_path_assignment(self) -> None:
        prose = "the user flips per-overlay (`[overlays.<name>].require_human_approval_to_answer = false`)"
        assert len(_handedit_lines(prose)) == 1

    def test_ignores_descriptive_tier_reference(self) -> None:
        prose = 'the `autonomy = "full"` tier collapses the gates'
        assert _handedit_lines(prose) == []

    def test_ignores_cli_routed_instruction(self) -> None:
        prose = "raise the tier with `t3 <overlay> autonomy set full` — never hand-edit the setting directly"
        assert _handedit_lines(prose) == []

    def test_ignores_cli_show(self) -> None:
        prose = "read the resolved tier with `t3 <overlay> autonomy show`"
        assert _handedit_lines(prose) == []


class TestSkillTreeHasNoHandedit:
    def test_no_skill_instructs_handediting_the_autonomy_knob(self) -> None:
        offenders: list[str] = []
        for path in _iter_skill_files():
            for lineno, line in _handedit_lines(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(_SKILLS_DIR.parent)}:{lineno}: {line}")
        assert not offenders, (
            "Skill file(s) instruct hand-editing the autonomy / answer-approval "
            "knob directly; route through `t3 <overlay> autonomy set <level>` "
            "instead (souliane/teatree#1737):\n" + "\n".join(offenders)
        )
