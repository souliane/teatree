"""``select_changed_scenarios`` is parameterized by ``repo_root`` and the catalog (#3337).

The inner matcher (:func:`selection_for_changed`) already took both parameters; the entry
point the CLI exposes hardwired teatree's own root and the full union catalog, so a consuming
overlay could not reach it. These tests pin that the entry point now honors both keyword
arguments while every default preserves teatree's own lane behavior exactly.
"""

from pathlib import Path

import pytest

from teatree.eval import changed_scenarios as cs
from teatree.eval.models import EvalSpec


def _spec(
    name: str,
    source_path: Path,
    *,
    agent_path: str = "",
    agent_sections: tuple[str, ...] = (),
) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="",
        agent_path=agent_path,
        prompt="",
        matchers=(),
        source_path=source_path,
        agent_sections=agent_sections,
    )


class TestSpecsUnder:
    def test_keeps_specs_under_dir_and_drops_the_rest(self, tmp_path: Path) -> None:
        catalog = tmp_path / "catalog"
        specs = [
            _spec("mine", catalog / "a.yaml"),
            _spec("mine_nested", catalog / "sub" / "b.yaml"),
            _spec("theirs", tmp_path / "other" / "c.yaml"),
        ]
        kept = cs.specs_under(specs, catalog)
        assert sorted(s.name for s in kept) == ["mine", "mine_nested"]

    def test_empty_when_dir_holds_no_specs(self, tmp_path: Path) -> None:
        specs = [_spec("theirs", tmp_path / "core" / "a.yaml")]
        assert cs.specs_under(specs, tmp_path / "empty") == []


class TestSelectChangedScenariosParameters:
    def test_repo_root_and_specs_are_both_honored(self, tmp_path: Path) -> None:
        spec = _spec("only", tmp_path / "evals" / "scenarios" / "x.yaml")
        selection = cs.select_changed_scenarios(["evals/scenarios/x.yaml"], repo_root=tmp_path, specs=[spec])
        assert selection.names == ["only"]

    def test_wrong_repo_root_matches_nothing(self, tmp_path: Path) -> None:
        # The diff path is relative to tmp_path, but the caller declares a different root,
        # so the same path normalizes elsewhere and selects no scenario (the quiet-skip a
        # consuming repo hits when it forgets to pass its own root).
        spec = _spec("only", tmp_path / "evals" / "scenarios" / "x.yaml")
        selection = cs.select_changed_scenarios(
            ["evals/scenarios/x.yaml"], repo_root=tmp_path / "elsewhere", specs=[spec]
        )
        assert selection.names == []

    def test_defaults_fall_back_to_discovery_and_teatree_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No keyword arguments == today's behavior: discover_specs() over teatree's own root.
        sentinel = [_spec("discovered", cs.REPO_ROOT / "evals" / "scenarios" / "disc.yaml")]
        monkeypatch.setattr(cs, "discover_specs", lambda: sentinel)
        selection = cs.select_changed_scenarios(["evals/scenarios/disc.yaml"])
        assert selection.names == ["discovered"]


SKILL = "skills/rules/SKILL.md"


def _prose_specs(tmp_path: Path) -> list[EvalSpec]:
    catalog = tmp_path / "evals" / "scenarios"
    return [
        _spec("graded_a", catalog / "a.yaml", agent_path=SKILL, agent_sections=("Background Long Operations",)),
        _spec("graded_b", catalog / "b.yaml", agent_path=SKILL, agent_sections=("Background Long Operations",)),
        _spec("other_section", catalog / "c.yaml", agent_path=SKILL, agent_sections=("Clickable References",)),
        _spec("whole_file", catalog / "d.yaml", agent_path=SKILL),
        _spec("other_skill", catalog / "e.yaml", agent_path="skills/ship/SKILL.md"),
    ]


class TestProseSelection:
    """Editing the skill prose a scenario GRADES selects that scenario (#3944).

    The graded system prompt is a section of an agent-facing skill file, not the scenario
    YAML, so keying selection on the YAML alone reports PASS having proven nothing about
    the scenarios whose subject matter moved.
    """

    def test_editing_a_graded_section_selects_every_scenario_grading_it(self, tmp_path: Path) -> None:
        selection = cs.select_changed_scenarios(
            [SKILL],
            repo_root=tmp_path,
            specs=_prose_specs(tmp_path),
            changed_sections={SKILL: frozenset({"Background Long Operations"})},
        )
        assert selection.names == ["graded_a", "graded_b", "whole_file"]

    def test_editing_an_ungraded_section_selects_nothing_extra(self, tmp_path: Path) -> None:
        # "Temp File Safety" is graded by no scenario here, so only the whole-file scenario
        # — whose prompt IS the entire file — may be selected.
        selection = cs.select_changed_scenarios(
            [SKILL],
            repo_root=tmp_path,
            specs=_prose_specs(tmp_path),
            changed_sections={SKILL: frozenset({"Temp File Safety"})},
        )
        assert selection.names == ["whole_file"]

    def test_unknown_section_granularity_selects_every_scenario_grading_the_file(self, tmp_path: Path) -> None:
        # No diff given (the path-list contract every overlay still uses): granularity is
        # unknown, so the selection is fail-safe to MORE — never a silent miss.
        selection = cs.select_changed_scenarios([SKILL], repo_root=tmp_path, specs=_prose_specs(tmp_path))
        assert selection.names == ["graded_a", "graded_b", "other_section", "whole_file"]

    def test_untouched_skill_selects_nothing(self, tmp_path: Path) -> None:
        selection = cs.select_changed_scenarios(
            ["src/teatree/eval/models.py"], repo_root=tmp_path, specs=_prose_specs(tmp_path)
        )
        assert selection.names == []

    def test_yaml_edit_still_selects_its_own_scenario(self, tmp_path: Path) -> None:
        selection = cs.select_changed_scenarios(
            ["evals/scenarios/c.yaml"], repo_root=tmp_path, specs=_prose_specs(tmp_path)
        )
        assert selection.names == ["other_section"]

    def test_section_matches_outrank_whole_file_matches_under_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Alphabetical truncation alone would drop the section-matched scenario (``z_…``)
        # in favour of the broad ones — re-creating the silent miss the cap is not there to
        # cause. The tightest evidence must survive the cap.
        monkeypatch.setattr(cs, "MAX_SELECTIVE_PR_SCENARIOS", 2)
        catalog = tmp_path / "evals" / "scenarios"
        specs = [
            _spec("a_whole", catalog / "a.yaml", agent_path=SKILL),
            _spec("b_whole", catalog / "b.yaml", agent_path=SKILL),
            _spec("z_section", catalog / "z.yaml", agent_path=SKILL, agent_sections=("Temp File Safety",)),
        ]
        selection = cs.select_changed_scenarios(
            [SKILL],
            repo_root=tmp_path,
            specs=specs,
            changed_sections={SKILL: frozenset({"Temp File Safety"})},
        )
        assert selection.names[0] == "z_section"
        assert selection.truncated
        assert selection.total_matched == 3
