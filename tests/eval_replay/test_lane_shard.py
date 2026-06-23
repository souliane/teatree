"""Each emitted metered-eval leg meters a budget-safe shard (souliane/teatree#2492, #2683).

The catalog is NOT evenly split across lanes — clean_room is ~182 scenarios,
under_load is 14 — so fanning out one leg per *lane* leaves a 182-scenario
clean_room leg that hits the same 80min wall the full suite did. The fix shards
each lane into contiguous slices of at most the lane's per-scenario-cost-aware
ceiling. These tests pin the structural invariants that keep the fix honest:
every emitted shard is within its lane's budget-safe bound, AND the shards are a
clean partition of the lane (every scenario in exactly one shard, none dropped or
duplicated).

The two lanes have wildly different per-scenario cost (#2683): a clean_room
scenario loads ONE skill into an empty context (seconds-to-a-minute), but an
under_load scenario loads the FULL skill bundle, a polluted context preamble, and
spawns a multi-agent roster — 10-45 minutes per scenario at 3 trials. So the
per-shard ceiling is LANE-AWARE: clean_room keeps the 14 ceiling, under_load
drops to a much smaller one so a roster-spawning shard fits the 80min cap.
"""

from pathlib import Path

import pytest

from teatree.eval.discovery import discover_specs
from teatree.eval.lane_shard import (
    MAX_SCENARIOS_PER_SHARD,
    UNDER_LOAD_MAX_SCENARIOS_PER_SHARD,
    LaneShard,
    ShardSpecError,
    filter_specs_by_shard,
    max_scenarios_per_shard,
    parse_shard,
    plan_lane_shards,
    shard_count_for,
)
from teatree.eval.models import CLEAN_ROOM_LANE, PERMITTED_LANES, UNDER_LOAD_LANE, EvalSpec


def _spec(name: str, lane: str = CLEAN_ROOM_LANE) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path="a",
        prompt="p",
        matchers=(),
        source_path=Path("x.yaml"),
        lane=lane,
    )


def _real_lane_specs(lane: str) -> list[EvalSpec]:
    return [spec for spec in discover_specs() if spec.lane == lane]


class TestParseShard:
    def test_none_and_empty_mean_no_sharding(self) -> None:
        assert parse_shard(None) is None
        assert parse_shard("") is None
        assert parse_shard("   ") is None

    def test_parses_index_total(self) -> None:
        assert parse_shard("2/6") == (2, 6)
        assert parse_shard("1/1") == (1, 1)

    @pytest.mark.parametrize("bad", ["abc", "1", "1/", "/3", "1/2/3", "0/3", "4/3", "1/0", "-1/3"])
    def test_malformed_or_out_of_range_fails_loud(self, bad: str) -> None:
        with pytest.raises(ShardSpecError):
            parse_shard(bad)


class TestMaxScenariosPerShard:
    def test_clean_room_keeps_the_default_ceiling(self) -> None:
        assert max_scenarios_per_shard(CLEAN_ROOM_LANE) == MAX_SCENARIOS_PER_SHARD == 14

    def test_under_load_has_a_smaller_roster_aware_ceiling(self) -> None:
        # under_load scenarios spawn multi-agent rosters (10-45 min each at 3
        # trials), so the lane's per-shard ceiling is much smaller than clean_room's
        # — otherwise a single under_load shard blows the 80min cap (#2683).
        assert max_scenarios_per_shard(UNDER_LOAD_LANE) == UNDER_LOAD_MAX_SCENARIOS_PER_SHARD
        assert UNDER_LOAD_MAX_SCENARIOS_PER_SHARD < MAX_SCENARIOS_PER_SHARD

    def test_unknown_lane_falls_back_to_the_default_ceiling(self) -> None:
        assert max_scenarios_per_shard("some_future_lane") == MAX_SCENARIOS_PER_SHARD


class TestShardCountFor:
    def test_at_or_below_bound_is_one_shard(self) -> None:
        assert shard_count_for(0) == 1
        assert shard_count_for(1) == 1
        assert shard_count_for(MAX_SCENARIOS_PER_SHARD) == 1

    def test_above_bound_splits(self) -> None:
        assert shard_count_for(MAX_SCENARIOS_PER_SHARD + 1) == 2
        assert shard_count_for(167) == 12  # ceil(167 / 14)

    def test_under_load_lane_uses_the_smaller_ceiling(self) -> None:
        # 14 under_load scenarios over the smaller ceiling → several shards, not one.
        bound = UNDER_LOAD_MAX_SCENARIOS_PER_SHARD
        assert shard_count_for(bound, UNDER_LOAD_LANE) == 1
        assert shard_count_for(bound + 1, UNDER_LOAD_LANE) == 2
        assert shard_count_for(14, UNDER_LOAD_LANE) > 1


class TestFilterSpecsByShard:
    def test_no_shard_returns_specs_unchanged(self) -> None:
        specs = [_spec("b"), _spec("a")]
        assert filter_specs_by_shard(specs, None) is specs
        assert filter_specs_by_shard(specs, "") is specs

    def test_shards_partition_with_nothing_dropped_or_duplicated(self) -> None:
        specs = [_spec(f"s{n:03d}") for n in range(30)]
        total = 4
        collected: list[str] = []
        for index in range(1, total + 1):
            collected.extend(s.name for s in filter_specs_by_shard(specs, f"{index}/{total}"))
        assert sorted(collected) == sorted(s.name for s in specs)
        assert len(collected) == len(set(collected))  # no scenario in two shards

    def test_shard_sizes_differ_by_at_most_one(self) -> None:
        specs = [_spec(f"s{n:03d}") for n in range(30)]
        total = 4
        sizes = [len(filter_specs_by_shard(specs, f"{i}/{total}")) for i in range(1, total + 1)]
        assert max(sizes) - min(sizes) <= 1

    def test_partition_is_deterministic_by_name(self) -> None:
        forward = [_spec("c"), _spec("a"), _spec("b")]
        backward = list(reversed(forward))
        assert [s.name for s in filter_specs_by_shard(forward, "1/3")] == [
            s.name for s in filter_specs_by_shard(backward, "1/3")
        ]


class TestPlanLaneShards:
    def test_lane_at_or_below_bound_is_a_single_leg(self) -> None:
        specs = [_spec(f"s{n}", CLEAN_ROOM_LANE) for n in range(MAX_SCENARIOS_PER_SHARD)]
        legs = plan_lane_shards(specs, [CLEAN_ROOM_LANE])
        assert legs == [LaneShard(lane=CLEAN_ROOM_LANE, index=1, total=1)]

    def test_under_load_lane_splits_on_its_smaller_ceiling(self) -> None:
        # 14 roster-spawning under_load scenarios must split into multiple shards on
        # the lane's smaller ceiling — the regression #2683 fixes (the lane ran as a
        # single 1/1 leg and hit the 80min cap).
        specs = [_spec(f"s{n:02d}", UNDER_LOAD_LANE) for n in range(14)]
        legs = plan_lane_shards(specs, [UNDER_LOAD_LANE])
        assert len(legs) > 1, "under_load (14 roster-spawning scenarios) must shard, not run as one leg."
        for leg in legs:
            shard_specs = filter_specs_by_shard(specs, leg.shard)
            assert len(shard_specs) <= UNDER_LOAD_MAX_SCENARIOS_PER_SHARD

    def test_large_lane_splits_into_enough_shards(self) -> None:
        specs = [_spec(f"s{n:03d}") for n in range(167)]
        legs = plan_lane_shards(specs, [CLEAN_ROOM_LANE])
        assert len(legs) == 12
        assert all(leg.total == 12 for leg in legs)
        assert [leg.index for leg in legs] == list(range(1, 13))

    def test_unknown_lane_fails_loud(self) -> None:
        with pytest.raises(ShardSpecError):
            plan_lane_shards([], ["bogus"])

    def test_matrix_entry_shape(self) -> None:
        assert LaneShard(lane=CLEAN_ROOM_LANE, index=2, total=12).as_matrix_entry() == {
            "lane": CLEAN_ROOM_LANE,
            "shard": "2/12",
        }


class TestEveryEmittedLegFitsTheBudget:
    """The blocking-finding fix: NO emitted job exceeds its lane's budget-safe bound.

    Run against the LIVE catalog (~196 = clean_room ~182 / under_load 14) so the
    test goes RED the moment the catalog grows past a lane's bound without the
    matrix re-sharding — exactly the silent budget-degrade #2492 warns about. The
    bound is LANE-AWARE (#2683): under_load's roster-spawning scenarios use a much
    smaller ceiling than clean_room's.
    """

    def test_no_emitted_shard_exceeds_the_budget_safe_bound(self) -> None:
        specs = discover_specs()
        legs = plan_lane_shards(specs, sorted(PERMITTED_LANES))
        for leg in legs:
            lane_specs = [spec for spec in specs if spec.lane == leg.lane]
            shard_specs = filter_specs_by_shard(lane_specs, leg.shard)
            bound = max_scenarios_per_shard(leg.lane)
            assert len(shard_specs) <= bound, (
                f"leg {leg.lane} {leg.shard} meters {len(shard_specs)} scenarios, over the "
                f"lane's budget-safe bound {bound}; re-shard the lane in lane_matrix.py."
            )

    @pytest.mark.parametrize("lane", sorted(PERMITTED_LANES))
    def test_shards_partition_the_real_lane_cleanly(self, lane: str) -> None:
        lane_specs = _real_lane_specs(lane)
        legs = plan_lane_shards(discover_specs(), [lane])
        collected: list[str] = []
        for leg in legs:
            collected.extend(s.name for s in filter_specs_by_shard(lane_specs, leg.shard))
        assert sorted(collected) == sorted(s.name for s in lane_specs), (
            f"{lane} shards are not a clean partition: a scenario was dropped or duplicated."
        )
        assert len(collected) == len(set(collected)), f"{lane}: a scenario landed in two shards."

    def test_the_dominant_clean_room_lane_is_actually_split(self) -> None:
        # Guard the exact regression the cold review caught: clean_room is the
        # dominant lane (~92% of the catalog) and MUST be split, not emitted as one
        # giant leg.
        legs = plan_lane_shards(discover_specs(), [CLEAN_ROOM_LANE])
        assert len(legs) > 1, "clean_room (the dominant ~182-scenario lane) must be sharded, not one leg."

    def test_the_under_load_lane_is_actually_split(self) -> None:
        # Guard the #2683 regression: under_load's roster-spawning scenarios are
        # 10-45 min each, so the lane MUST split into multiple shards rather than run
        # as one 1/1 leg that hits the 80min cap (run 27995563148).
        legs = plan_lane_shards(discover_specs(), [UNDER_LOAD_LANE])
        assert len(legs) > 1, "under_load (roster-spawning, 10-45 min/scenario) must be sharded, not one 1/1 leg."
