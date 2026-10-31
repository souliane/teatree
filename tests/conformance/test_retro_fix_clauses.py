"""The skills that turn a waste into a fix carry the clauses the retro fixes depend on."""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

WASTE_RECORDED_WITH_COST = "Every waste is recorded with its cost before it becomes a fix."

_CLAUSES = (
    ("skills/retro/SKILL.md", WASTE_RECORDED_WITH_COST),
    ("skills/dreaming/SKILL.md", WASTE_RECORDED_WITH_COST),
    ("skills/test/SKILL.md", "A regression that only reproduces in a primary checkout is verified in one."),
    (
        "skills/code/SKILL.md",
        'A guard that cannot distinguish "nothing matched" from "nothing was read" reports a false clean',
    ),
    ("skills/rules/references/sub-agents.md", "A killed headless run resumes once an account is usable."),
)


@pytest.mark.parametrize(("relative_path", "clause"), _CLAUSES, ids=[path for path, _ in _CLAUSES])
def test_skill_carries_its_pinned_clause(relative_path: str, clause: str) -> None:
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert clause in " ".join(text.split())
