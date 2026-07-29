"""The engagement skill's Related-Skills table and the ``requires`` edges must agree.

``skills/interactive/SKILL.md`` states that each mode-specific skill it lists
``require:``s it, "so it loads alongside". That contract is what makes the
engagement skill reachable at all — nothing else loads it, so a listed skill
missing the edge silently leaves that mode UNENGAGED — `.teatree-active` is
never written, so `_loop_auto_load_active()` cannot arm the loop or statusline.

It has already rotted once: souliane/teatree#3634 retired
``skills/teatree-batch`` (which carried the engagement edge) and moved its
per-ticket delivery cycle into ``skills/wip``, without carrying the edge over.
The table kept advertising the contract while the wiring was gone. A prose
table cannot enforce itself — this walk does.
"""

import re
from pathlib import Path

from teatree.skill_support.requires_parser import parse_requires

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
ENGAGEMENT_SKILL = "interactive"

_TABLE_ROW = re.compile(r"^\|\s*`/([\w:-]+)`\s*\|")


def _related_skill_names() -> set[str]:
    """The skill names listed in the engagement skill's Related Skills table."""
    text = (SKILLS_DIR / ENGAGEMENT_SKILL / "SKILL.md").read_text(encoding="utf-8")
    section = text.partition("## Related Skills")[2]
    return {match.group(1).rpartition(":")[2] for line in section.splitlines() if (match := _TABLE_ROW.match(line))}


def test_related_skills_table_is_not_empty():
    assert _related_skill_names(), "Related Skills table parsed to nothing — the row regex has drifted"


def test_every_related_skill_requires_the_engagement_skill():
    missing = sorted(
        name
        for name in _related_skill_names()
        if (skill_md := SKILLS_DIR / name / "SKILL.md").is_file()
        and ENGAGEMENT_SKILL not in (parse_requires(skill_md.read_text(encoding="utf-8")) or [])
    )
    assert not missing, (
        f"skills/{ENGAGEMENT_SKILL}/SKILL.md lists these as requiring it, but they do not declare "
        f"`requires: {ENGAGEMENT_SKILL}`: {missing}. Nothing else loads the engagement skill, so each "
        f"listed skill is its only route in — add the edge or drop the row."
    )


def test_every_related_skill_exists():
    absent = sorted(name for name in _related_skill_names() if not (SKILLS_DIR / name / "SKILL.md").is_file())
    assert not absent, f"Related Skills table names skills with no SKILL.md: {absent}"
