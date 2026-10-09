"""Where a discovered scenario's replay fixtures are looked for."""

from pathlib import Path

from teatree.eval.discovery import FIXTURES_DIR, SCENARIOS_DIR, discover_core_specs, fixture_dir_for
from teatree.eval.models import EvalSpec


def _spec_at(source_path: Path) -> EvalSpec:
    return EvalSpec(
        name="synthetic",
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="synthetic",
        matchers=(),
        source_path=source_path,
    )


class TestFixtureDirFor:
    def test_a_core_scenario_resolves_to_the_core_fixtures_dir(self) -> None:
        assert fixture_dir_for(_spec_at(SCENARIOS_DIR / "rules.yaml")) == FIXTURES_DIR

    def test_an_overlay_scenario_resolves_beside_its_own_yaml(self, tmp_path: Path) -> None:
        assert fixture_dir_for(_spec_at(tmp_path / "overlay_scenarios.yaml")) == tmp_path / "fixtures"

    def test_every_shipped_core_scenario_still_reads_the_core_dir(self) -> None:
        assert {fixture_dir_for(spec) for spec in discover_core_specs()} == {FIXTURES_DIR}
