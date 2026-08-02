"""Every skill name teatree can put in front of an agent must exist in the catalogue.

A suggestion the catalogue cannot resolve is worse than no suggestion: the
skill-loading gate warns the name is unresolvable and falls open, or — when a
stale directory of that name survives on some box's ``~/.agents/skills`` —
hard-blocks every ``Bash``/``Edit``/``Write`` call on a skill nobody can load.
Either way the agent learns to reach for the ``[skill-load-ok:]`` override,
which is how a real gate stops being obeyed.

The names below are constants, so every test asserting through the constant
(``autoload_skill_demand([]) == [PLATFORM_SKILL]``) stays green across a rename
and can never catch this. Resolution against the shipped ``skills/`` tree is
what does.
"""

from pathlib import Path

import pytest

from hooks.scripts.engagement import LIFECYCLE_SEED_SKILLS, PLATFORM_SKILL
from teatree.skill_support.loading import _PHASE_TO_SKILL, _STATUS_TO_SKILL, INTERNALS_SKILL_NAME

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def _skill_dir_name(name: str) -> str:
    return name.rpartition(":")[2]


def _from(source: str, names: tuple[str, ...] | list[str]) -> list[tuple[str, str]]:
    return [(source, name) for name in names]


SUGGESTED_NAMES = [
    *_from("engagement.PLATFORM_SKILL", [PLATFORM_SKILL]),
    *_from("engagement.LIFECYCLE_SEED_SKILLS", LIFECYCLE_SEED_SKILLS),
    *_from("loading.INTERNALS_SKILL_NAME", [INTERNALS_SKILL_NAME]),
    *_from("loading._STATUS_TO_SKILL", sorted(set(_STATUS_TO_SKILL.values()))),
    *_from("loading._PHASE_TO_SKILL", sorted(set(_PHASE_TO_SKILL.values()))),
]


def test_the_catalogue_is_where_this_test_thinks_it_is() -> None:
    assert (SKILLS_DIR / "rules" / "SKILL.md").is_file(), (
        f"{SKILLS_DIR} is not the skills catalogue — every resolution below would be vacuously true"
    )


@pytest.mark.parametrize(("source", "name"), SUGGESTED_NAMES, ids=lambda value: value)
def test_every_suggested_skill_name_resolves(source: str, name: str) -> None:
    assert (SKILLS_DIR / _skill_dir_name(name) / "SKILL.md").is_file(), (
        f"{source} names `{name}`, which has no skills/{_skill_dir_name(name)}/SKILL.md. "
        f"A suggestion the catalogue cannot resolve teaches every lane to reach for the "
        f"`[skill-load-ok:]` override — point it at the skill that replaced it, or drop it."
    )
