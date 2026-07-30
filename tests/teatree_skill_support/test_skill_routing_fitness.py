"""Deterministic fitness test for the skill-routing tables.

Every skill name a routing table can emit must resolve to a real skill
directory carrying a ``SKILL.md``. A dead route (a phase/status/keyword that
maps to a skill name with no directory) slipped through before because nothing
walked the tables against the on-disk skill tree — this test is that walk.

The fitness test treats the routing tables as the source of truth and the skill
tree (``skills/`` and the ``plugins/t3/skills/`` mirror) as the resolver. It is
intentionally exhaustive: a new route added to any table is automatically
covered the moment it lands.
"""

from __future__ import annotations  # noqa: TID251 — pure-logic fitness test

from pathlib import Path

import pytest

from teatree.skill_support.loading import _PHASE_TO_SKILL, _STATUS_TO_SKILL

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_SEARCH_DIRS = (
    _REPO_ROOT / "skills",
    _REPO_ROOT / "plugins" / "t3" / "skills",
)


def _resolves_to_skill(name: str) -> bool:
    """True when ``name`` is a skill directory with a SKILL.md in any search dir."""
    return any((base / name / "SKILL.md").is_file() for base in _SKILL_SEARCH_DIRS)


def _routed_skill_names() -> set[str]:
    """Every skill name the routing tables can emit.

    The values of the phase/status maps are the skill targets the loader can
    hand back for an explicit ``--phase`` or a ticket status.
    """
    names: set[str] = set()
    names.update(_PHASE_TO_SKILL.values())
    names.update(_STATUS_TO_SKILL.values())
    return names


class TestRoutingTablesResolveToSkills:
    def test_at_least_one_search_dir_exists(self):
        assert any(base.is_dir() for base in _SKILL_SEARCH_DIRS), (
            "no skills/ tree found — the fitness test cannot resolve any route"
        )

    @pytest.mark.parametrize("skill_name", sorted(_routed_skill_names()))
    def test_routed_skill_resolves_to_directory(self, skill_name: str):
        assert _resolves_to_skill(skill_name), (
            f"routing table emits skill {skill_name!r} but no "
            f"skills/{skill_name}/SKILL.md exists in {[str(b) for b in _SKILL_SEARCH_DIRS]}"
        )

    def test_phase_to_skill_planning_route_is_live(self):
        # Direct guard on the route that slipped before: the planning phase must
        # map to an existing skill, not a dead name.
        target = _PHASE_TO_SKILL["planning"]
        assert _resolves_to_skill(target), f"_PHASE_TO_SKILL['planning'] -> {target!r} does not resolve to a skill"
