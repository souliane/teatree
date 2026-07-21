"""Model-tier PRESETS: the composition layer over ``resolve_eval_model``.

Pure unit tests — no CLI, no runner. Precedence (with a preset active):
``spec.model`` > the preset's own entry > (per-scenario preset only) fall
through to :func:`resolve_eval_model` (the scenario's own ``tier``/``phase``/
default tier).
"""

from pathlib import Path

import pytest

from teatree.agents.model_tiering import TIER_MODELS
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.models import EvalSpec
from teatree.eval.presets import (
    CHEAP_PRESET,
    FRONTIER_PRESET,
    Preset,
    PresetError,
    baseline_preset,
    known_preset_names,
    load_baseline_file,
    resolve_preset,
    resolve_preset_model,
)


def _spec(*, name: str = "s", model: str = "", tier: str = "", phase: str = "") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="sc",
        agent_path="skills/code/SKILL.md",
        prompt="p",
        matchers=(),
        source_path=Path("evals/scenarios/x.yaml"),
        model=model,
        tier=tier,
        phase=phase,
    )


class TestPresetConstruction:
    def test_uniform_tier_only_is_valid(self) -> None:
        preset = Preset(name="p", tier="cheap")
        assert preset.tier == "cheap"
        assert preset.scenario_tiers == {}

    def test_empty_scenario_map_with_no_tier_is_valid(self) -> None:
        # The freshly-committed baseline.yaml, before `set-baseline` populates it.
        preset = Preset(name="p", scenario_tiers={})
        assert preset.tier == ""

    def test_both_tier_and_scenario_tiers_is_rejected(self) -> None:
        with pytest.raises(PresetError, match="mutually exclusive"):
            Preset(name="p", tier="cheap", scenario_tiers={"a": "cheap"})


class TestResolvePresetModel:
    def test_explicit_spec_model_wins_over_a_uniform_preset(self) -> None:
        spec = _spec(model="claude-pinned-id@xhigh")
        assert resolve_preset_model(spec, CHEAP_PRESET) == "claude-pinned-id@xhigh"

    def test_explicit_spec_model_wins_over_a_per_scenario_preset_entry(self) -> None:
        spec = _spec(name="alpha", model="claude-pinned-id@xhigh")
        preset = Preset(name="p", scenario_tiers={"alpha": "frontier"})
        assert resolve_preset_model(spec, preset) == "claude-pinned-id@xhigh"

    def test_uniform_preset_forces_every_scenario_to_its_tier(self) -> None:
        for spec in (_spec(tier="frontier"), _spec(phase="planning"), _spec()):
            assert resolve_preset_model(spec, CHEAP_PRESET) == TIER_MODELS["cheap"]

    def test_frontier_preset_forces_the_frontier_tier(self) -> None:
        assert resolve_preset_model(_spec(), FRONTIER_PRESET) == TIER_MODELS["frontier"]

    def test_per_scenario_entry_wins_over_the_scenarios_own_tier(self) -> None:
        spec = _spec(name="alpha", tier="frontier")
        preset = Preset(name="p", scenario_tiers={"alpha": "cheap"})
        assert resolve_preset_model(spec, preset) == TIER_MODELS["cheap"]

    def test_absent_from_the_map_falls_through_to_the_scenarios_own_tier(self) -> None:
        # The scenario is never silently cheapened when it isn't in the map.
        spec = _spec(name="not_in_map", tier="frontier")
        preset = Preset(name="p", scenario_tiers={"other": "cheap"})
        assert resolve_preset_model(spec, preset) == resolve_eval_model(spec) == TIER_MODELS["frontier"]

    def test_absent_from_the_map_falls_through_to_the_default_tier(self) -> None:
        spec = _spec(name="not_in_map")
        preset = Preset(name="p", scenario_tiers={"other": "cheap"})
        assert resolve_preset_model(spec, preset) == resolve_eval_model(spec)

    def test_empty_map_preset_is_a_pure_fallthrough_for_every_scenario(self) -> None:
        preset = Preset(name="p", scenario_tiers={})
        for spec in (_spec(tier="cheap"), _spec(phase="testing"), _spec()):
            assert resolve_preset_model(spec, preset) == resolve_eval_model(spec)


class TestLoadBaselineFile:
    def test_missing_file_is_fail_loud(self, tmp_path: Path) -> None:
        with pytest.raises(PresetError, match="missing"):
            load_baseline_file(tmp_path / "does_not_exist.yaml")

    def test_non_mapping_top_level_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(PresetError, match="mapping"):
            load_baseline_file(path)

    def test_scenarios_not_a_mapping_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("scenarios: [a, b]\n", encoding="utf-8")
        with pytest.raises(PresetError, match="scenarios"):
            load_baseline_file(path)

    def test_unknown_tier_value_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("scenarios:\n  alpha: not_a_real_tier\n", encoding="utf-8")
        with pytest.raises(PresetError, match="unknown tier"):
            load_baseline_file(path)

    def test_frontier_without_frontier_ok_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("scenarios:\n  alpha: frontier\n", encoding="utf-8")
        with pytest.raises(PresetError, match="frontier_ok"):
            load_baseline_file(path)

    def test_frontier_ok_scenario_pinning_frontier_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("scenarios:\n  alpha: frontier\nfrontier_ok:\n  - alpha\n", encoding="utf-8")
        parsed = load_baseline_file(path)
        assert parsed.scenario_tiers == {"alpha": "frontier"}
        assert parsed.frontier_ok == frozenset({"alpha"})

    def test_frontier_ok_not_a_list_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("scenarios: {}\nfrontier_ok: alpha\n", encoding="utf-8")
        with pytest.raises(PresetError, match="frontier_ok"):
            load_baseline_file(path)

    def test_empty_file_parses_to_an_empty_map(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("scenarios: {}\nfrontier_ok: []\n", encoding="utf-8")
        parsed = load_baseline_file(path)
        assert parsed.scenario_tiers == {}
        assert parsed.frontier_ok == frozenset()


class TestBaselinePreset:
    def test_wraps_the_parsed_file_into_a_preset(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        path.write_text("scenarios:\n  alpha: cheap\n  beta: balanced\n", encoding="utf-8")
        preset = baseline_preset(path)
        assert preset.name == "baseline"
        assert preset.scenario_tiers == {"alpha": "cheap", "beta": "balanced"}
        assert preset.tier == ""


class TestResolvePreset:
    def test_cheap_and_frontier_resolve_to_the_shipped_uniform_presets(self) -> None:
        assert resolve_preset("cheap") is CHEAP_PRESET
        assert resolve_preset("frontier") is FRONTIER_PRESET

    def test_baseline_resolves_from_the_checked_in_file(self) -> None:
        # The checked-in evals/presets/baseline.yaml is committed empty — a
        # fresh, unpopulated per-scenario preset with no entries.
        preset = resolve_preset("baseline")
        assert preset.name == "baseline"
        assert isinstance(preset.scenario_tiers, dict)

    def test_unknown_name_names_the_known_presets(self) -> None:
        with pytest.raises(PresetError, match="unknown preset"):
            resolve_preset("does-not-exist")

    def test_known_preset_names_lists_all_three(self) -> None:
        assert set(known_preset_names()) == {"cheap", "frontier", "baseline"}
