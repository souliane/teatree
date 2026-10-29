"""The check-in walk-through must surface and triage every owner wait."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from teatree.core.checking import CheckGroup, CheckingReport

SKILL = Path(__file__).resolve().parents[2] / "skills/checking/SKILL.md"


@pytest.mark.parametrize(
    "terms",
    [
        ("record every wait already known", "held pull request", "gate refusal", "approval request"),
        ("join by age, oldest first", "n left"),
        ("complete non-row list", "caps its rows at five", "exclude its `question` entries"),
        ("re-read the facts", "dismiss with evidence", "earlier answer"),
        ("owner-kept decision", "spending money", "irreversible deletion", "credential use", "colleague-visible posts"),
        ("report each automatic resolution", "in one line", "overrule it"),
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


ROOT = Path(__file__).resolve().parents[2]


def test_documented_group_order_matches_report() -> None:
    skill = (ROOT / "skills/checking/SKILL.md").read_text()
    blueprint = (ROOT / "BLUEPRINT.md").read_text()
    report = CheckingReport(
        since=datetime.now(UTC),
        merged=CheckGroup("Merged"),
        in_flight=CheckGroup("In-flight"),
        needs_you=CheckGroup("Needs you"),
        metered=CheckGroup("Metered"),
    )
    names = [group.title for group in report.groups]
    assert names == ["Merged", "In-flight", "Needs you", "Metered", "Remaining"]
    line = next(line for line in skill.splitlines() if "Groups in fixed order" in line)
    assert ", ".join(f"`{name}`" for name in names) in line
    assert "Five groups: Merged / In-flight / Needs you / Metered / Remaining" in blueprint


def test_checking_walk_posts_slack_questions_before_any_direct_fallback() -> None:
    skill = SKILL.read_text().lower()
    walk = skill.split("## walking the owner waits", 1)[1].split("## the single command", 1)[0]
    assert "checking shoot" in walk
    assert "slack permalink" in walk
    assert "only when slack is broken" in walk
    assert "otherwise raise one card at a time through `askuserquestion`" not in walk
