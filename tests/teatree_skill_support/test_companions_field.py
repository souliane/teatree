"""The restored ``companions:`` field is a SOFT suggestion, distinct from hard ``requires``.

``requires`` is enforced (pulled into the transitive load chain); ``companions``
is only surfaced. These pin that distinction end-to-end: at the resolver
(:func:`resolve_requires` vs :func:`companion_suggestions`) and at the loading
policy (``select_for_prompt_hook`` returns companions in ``companion_suggestions``,
never in the demanded ``skills``).
"""

from pathlib import Path

from teatree.skill_support.deps import companion_suggestions, resolve_requires
from teatree.skill_support.loading import SkillLoadingPolicy

_INDEX = [
    {"skill": "code", "requires": ["rules"], "companions": ["writing-plans"]},
    {"skill": "rules", "requires": [], "companions": []},
]


class TestResolverSoftVsHard:
    def test_requires_stays_mandatory_and_companions_are_not_enforced(self) -> None:
        resolved = resolve_requires(["code"], _INDEX)
        assert "rules" in resolved  # requires is the hard, enforced edge
        assert "code" in resolved
        assert "writing-plans" not in resolved  # a companion is never folded into the chain

    def test_companion_suggestions_surfaces_the_soft_companion(self) -> None:
        resolved = resolve_requires(["code"], _INDEX)
        assert companion_suggestions(resolved, _INDEX) == ["writing-plans"]

    def test_companion_already_hard_required_is_not_double_surfaced(self) -> None:
        index = [
            {"skill": "code", "requires": ["rules"], "companions": ["rules", "writing-plans"]},
            {"skill": "rules", "requires": [], "companions": []},
        ]
        resolved = resolve_requires(["code"], index)
        assert companion_suggestions(resolved, index) == ["writing-plans"]

    def test_companions_are_not_transitive(self) -> None:
        # A companion's own requires/companions are not pulled in — it is a
        # suggestion, not a dependency.
        index = [
            {"skill": "code", "requires": [], "companions": ["a"]},
            {"skill": "a", "requires": ["b"], "companions": ["c"]},
        ]
        resolved = resolve_requires(["code"], index)
        assert companion_suggestions(resolved, index) == ["a"]


class TestPromptHookSurfacesCompanionsSoftly:
    def test_companion_is_surfaced_but_not_a_hard_demand(self, tmp_path: Path) -> None:
        (tmp_path / "manage.py").write_text("# django project\n", encoding="utf-8")
        index = [{"skill": "ac-django", "requires": [], "companions": ["ac-python"]}]
        result = SkillLoadingPolicy().select_for_prompt_hook(
            cwd=tmp_path,
            overlay_skill_metadata={},
            loaded_skills=set(),
            skill_index=index,
        )
        assert "ac-django" in result.skills  # requires/detection is the hard demand set
        assert "ac-python" not in result.skills  # a companion is never a hard demand
        assert "ac-python" in result.companion_suggestions  # surfaced softly instead
