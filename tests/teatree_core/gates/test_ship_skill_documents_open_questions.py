"""ship/SKILL.md must document the Open-questions-section requirement (#1933).

Doctrine: any open question (solved or not) and any non-explicit assumption is
listed in BOTH the commit message body AND the PR description under an "Open
questions & assumptions" section. ship/SKILL.md § 5 is the single source of
truth; code/SKILL.md cross-references it. This doc-invariant guard catches a
skill that drifts out of sync with the live ``open_questions_gate`` warn on
every CI run (in the spirit of ``test_ship_skill_documents_skip_flags``).
"""

from pathlib import Path

from teatree.core.gates.open_questions_gate import OPEN_QUESTIONS_HINT

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHIP_SKILL = _REPO_ROOT / "skills" / "ship" / "SKILL.md"
_CODE_SKILL = _REPO_ROOT / "skills" / "code" / "SKILL.md"


def test_ship_skill_documents_section_and_statuses() -> None:
    text = _SHIP_SKILL.read_text(encoding="utf-8")
    assert "Open Questions & Assumptions" in text
    assert "Open questions & assumptions" in text
    for status in ("decided-by-user", "assumed", "open"):
        assert status in text


def test_ship_skill_names_the_gate_module() -> None:
    text = _SHIP_SKILL.read_text(encoding="utf-8")
    assert "teatree.core.gates.open_questions_gate" in text


def test_ship_skill_commit_section_requires_the_section() -> None:
    text = _SHIP_SKILL.read_text(encoding="utf-8")
    assert "Open questions & assumptions` section in the commit message body" in text


def test_code_skill_cross_references_ship() -> None:
    text = _CODE_SKILL.read_text(encoding="utf-8")
    assert "Open questions & assumptions" in text
    assert "ship/SKILL.md" in text


def test_hint_constant_is_actionable() -> None:
    assert "Open questions" in OPEN_QUESTIONS_HINT
