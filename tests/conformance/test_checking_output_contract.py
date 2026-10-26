"""The check-in walk-through must surface and triage every owner wait."""

from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills/checking/SKILL.md"


@pytest.mark.parametrize(
    "terms",
    [
        ("record every wait already known", "held pull request", "gate refusal", "approval request"),
        ("join by age, oldest first", "n left"),
        ("complete non-row list", "caps its rows at five", "exclude its `question` entries"),
        ("re-read the facts", "dismiss with evidence", "earlier answer"),
        ("owner-kept decision", "spending money", "irreversible deletion", "credential use", "colleague-visible posts"),
        ("report each automatic resolution", "overrule it"),
        ("awaiting your slack reply", "question_answer", "refuse it"),
        ("zero **untriaged** waits", "name each still-open card"),
    ],
)
def test_walk_through_contract(terms: tuple[str, ...]) -> None:
    text = (
        SKILL.read_text()
        .lower()
        .split("## walking the owner waits (no mode flip)", 1)[1]
        .split("## the single command", 1)[0]
    )
    assert all(term in text for term in terms)
