"""``evals/presets/baseline.yaml`` hygiene — the checked-in ``baseline`` preset file.

Every key must be a real discovered scenario (a scenario renamed or removed
must be pruned by the next ``t3 eval set-baseline`` run, not left stale), every
value a known ``TIER_MODELS`` key, and no ``frontier`` value unless the
scenario is listed under ``frontier_ok``.
"""

from teatree.agents.model_tiering import TIER_MODELS
from teatree.eval.discovery import discover_specs
from teatree.eval.presets import BASELINE_PRESET_PATH, load_baseline_file


class TestBaselineYamlHygiene:
    def test_loads_without_error(self) -> None:
        # load_baseline_file itself rejects an unknown tier value and a
        # frontier-valued entry absent from frontier_ok — a clean load already
        # proves both invariants hold for the checked-in file.
        load_baseline_file(BASELINE_PRESET_PATH)

    def test_every_key_is_a_real_discovered_scenario(self) -> None:
        parsed = load_baseline_file(BASELINE_PRESET_PATH)
        discovered = {spec.name for spec in discover_specs()}
        unknown = set(parsed.scenario_tiers) - discovered
        assert not unknown, f"baseline.yaml names scenarios no longer discovered: {sorted(unknown)}"

    def test_every_value_is_a_known_tier(self) -> None:
        parsed = load_baseline_file(BASELINE_PRESET_PATH)
        bad = {name: tier for name, tier in parsed.scenario_tiers.items() if tier not in TIER_MODELS}
        assert not bad, f"baseline.yaml declares unknown tiers: {bad}"

    def test_no_frontier_value_unless_listed_in_frontier_ok(self) -> None:
        parsed = load_baseline_file(BASELINE_PRESET_PATH)
        frontier_scenarios = {name for name, tier in parsed.scenario_tiers.items() if tier == "frontier"}
        unapproved = frontier_scenarios - parsed.frontier_ok
        assert not unapproved, f"baseline.yaml pins frontier without frontier_ok approval: {sorted(unapproved)}"
