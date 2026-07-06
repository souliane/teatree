"""Regression coverage for the eval skill-catalog fixture gap.

CI run 28630941573 (shard ``clean_room`` 8/15 and 9/15) reproducibly failed
eight ``overlay_*``/``non_overlay_*`` skill-routing scenarios: each prompt
references an overlay-placeholder or companion-bible skill name with a leading
slash (``/t3-widget``, ``/widget-le``, ``/backend-dev``, ``/ac-django``, ...),
or a name the agent must DERIVE (``/ac-python``, the review skill), but the
name never appeared in the simulated agent's ambient "available skills" list
during the eval run — so the agent's own "only invoke a listed skill" refusal
rule correctly declined to call it. The fix widens the simulated catalog
(``EvalSpec.available_skills`` -> ``ClaudeAgentOptions.skills``/``plugins`` via
``evals/fixtures/skill_catalog``, see ``teatree.eval.api_runner``) rather than
weakening that refusal rule.

This module pins the FIXED STATE for each of the eight named scenarios: every
skill name its prompt references now round-trips through the loader into
``available_skills`` and resolves to a real fixture skill directory the SDK can
genuinely discover.
"""

from pathlib import Path

from teatree.eval.api_runner import (
    CleanRoomConfig,
    _qualify_catalog_skill,
    _skill_catalog_fixture_plugin,
    build_sdk_options,
)
from teatree.eval.discovery import discover_specs

#: The eight scenarios CI run 28630941573 reported failing, each mapped to the
#: skill name(s) its prompt references that core does not itself ship. A name
#: appearing here must appear in that scenario's ``available_skills`` AND have
#: a real fixture skill directory — both halves of the fix.
_EXPECTED_REFERENCED_SKILLS: dict[str, frozenset[str]] = {
    "overlay_repo_task_loads_overlay_skill": frozenset({"t3-widget", "backend-dev"}),
    "overlay_review_loads_overlay_review_skill_set": frozenset({"t3-widget", "widget-le", "backend-dev"}),
    "non_overlay_task_does_not_require_overlay_skill": frozenset({"ac-django"}),
    "overlay_django_coding_loads_companion_bible": frozenset({"t3-widget", "backend-dev", "ac-django"}),
    "overlay_python_coding_generalizes_to_python_bible": frozenset({"t3-widget", "backend-dev", "ac-python"}),
    "overlay_review_generalizes_to_declared_skill_set": frozenset({"t3-widget"}),
    "non_overlay_review_does_not_load_overlay_skill": frozenset({"review"}),
    "overlay_repo_review_loads_overlay_skill_first": frozenset({"t3-widget"}),
}


def _specs_by_name() -> dict[str, list]:
    specs = discover_specs()
    by_name: dict[str, list] = {}
    for spec in specs:
        by_name.setdefault(spec.name, []).append(spec)
    return by_name


class TestEightFailingScenariosDeclareTheirReferencedSkills:
    def test_all_eight_scenarios_are_present_in_the_catalog(self) -> None:
        # Anti-vacuity: if a scenario was renamed/removed, the rest of this
        # module would vacuously pass over an empty set — fail loud instead.
        by_name = _specs_by_name()
        missing = set(_EXPECTED_REFERENCED_SKILLS) - set(by_name)
        assert not missing, f"expected failing scenario(s) not found in the catalog: {sorted(missing)}"

    def test_each_scenario_declares_available_skills_covering_every_referenced_name(self) -> None:
        by_name = _specs_by_name()
        for name, referenced in _EXPECTED_REFERENCED_SKILLS.items():
            (spec,) = by_name[name]
            declared = set(spec.available_skills)
            missing = referenced - declared
            assert not missing, f"{name}: available_skills is missing referenced skill(s) {sorted(missing)}"

    def test_no_scenario_is_left_with_an_empty_available_skills(self) -> None:
        # Every one of the eight must have SOME widening declared — an empty
        # available_skills means the catalog gap is still open for that name.
        by_name = _specs_by_name()
        for name in _EXPECTED_REFERENCED_SKILLS:
            (spec,) = by_name[name]
            assert spec.available_skills, f"{name}: available_skills is still empty"


class TestReferencedSkillsResolveToRealFixtureDirectories:
    """The widening must be REAL, not just a declared name with nothing behind it."""

    def test_every_referenced_skill_has_a_fixture_skill_md(self) -> None:
        plugin_dir = Path(_skill_catalog_fixture_plugin()["path"])
        fixture_names = {p.parent.name for p in plugin_dir.glob("skills/*/SKILL.md")}
        every_referenced: set[str] = set()
        for referenced in _EXPECTED_REFERENCED_SKILLS.values():
            every_referenced.update(referenced)
        missing = every_referenced - fixture_names
        assert not missing, f"referenced skill(s) have no fixture SKILL.md: {sorted(missing)}"

    def test_fixture_plugin_manifest_is_valid_json_with_a_name(self) -> None:
        import json  # noqa: PLC0415

        plugin_dir = Path(_skill_catalog_fixture_plugin()["path"])
        manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert manifest.get("name")

    def test_each_fixture_skill_md_has_a_matching_frontmatter_name(self) -> None:
        # The SDK's `skills` filter matches "the SKILL.md name / directory
        # name" — a mismatched frontmatter `name:` would silently break the
        # widening for a caller that filters on the frontmatter value.
        plugin_dir = Path(_skill_catalog_fixture_plugin()["path"])
        for skill_md in plugin_dir.glob("skills/*/SKILL.md"):
            text = skill_md.read_text(encoding="utf-8")
            assert text.startswith("---\n"), skill_md
            frontmatter = text[3 : text.index("---", 3)]
            name_line = next((line for line in frontmatter.splitlines() if line.startswith("name:")), None)
            assert name_line is not None, f"{skill_md}: no `name:` in frontmatter"
            assert name_line.split(":", 1)[1].strip() == skill_md.parent.name


class TestClaudeAgentOptionsCarryTheWidenedCatalog:
    """End-to-end: the loaded spec's available_skills reaches the SDK options."""

    def test_build_sdk_options_lists_exactly_the_declared_names(self, tmp_path: Path) -> None:
        # The CLI lists a plugin's skills under the plugin-qualified
        # `<plugin>:<skill>` key, so the options filter carries the qualified
        # form of each declared name (see `_qualify_catalog_skill`) — the
        # unambiguous canonical key that matches the CLI's own listing.
        by_name = _specs_by_name()
        (spec,) = by_name["overlay_django_coding_loads_companion_bible"]
        config = CleanRoomConfig(
            system_prompt="sp",
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Skill", "Bash", "Edit"),
            model="haiku",
            max_turns=5,
            skills=spec.available_skills,
        )
        options = build_sdk_options(config)
        assert set(options.skills or []) == {_qualify_catalog_skill(name) for name in spec.available_skills}
        assert len(options.plugins) == 1
