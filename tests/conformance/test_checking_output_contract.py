from datetime import UTC, datetime
from pathlib import Path

from teatree.core.checking import CheckGroup, CheckingReport

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
