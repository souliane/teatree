"""Each emitted metered-eval leg meters a budget-safe shard (souliane/teatree#2492).

The catalog is NOT evenly split across lanes — clean_room is 167 scenarios,
under_load is 14 — so fanning out one leg per *lane* leaves a 167-scenario
clean_room leg that hits the same 80min wall the full suite did. The fix shards
each lane into contiguous slices of at most :data:`MAX_SCENARIOS_PER_SHARD`
scenarios. These tests pin the two structural invariants that keep the fix
honest: every emitted shard is within the budget-safe bound, AND the shards are a
clean partition of the lane (every scenario in exactly one shard, none dropped or
duplicated).
"""

from pathlib import Path

import pytest

from teatree.eval.discovery import discover_specs
from teatree.eval.lane_shard import (
    MAX_SCENARIOS_PER_SHARD,
    LaneShard,
    ShardSpecError,
    filter_specs_by_shard,
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


class TestShardCountFor:
    def test_at_or_below_bound_is_one_shard(self) -> None:
        assert shard_count_for(0) == 1
        assert shard_count_for(1) == 1
        assert shard_count_for(MAX_SCENARIOS_PER_SHARD) == 1

    def test_above_bound_splits(self) -> None:
        assert shard_count_for(MAX_SCENARIOS_PER_SHARD + 1) == 2
        assert shard_count_for(167) == 12  # ceil(167 / 14)


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
        specs = [_spec(f"s{n}", UNDER_LOAD_LANE) for n in range(MAX_SCENARIOS_PER_SHARD)]
        legs = plan_lane_shards(specs, [UNDER_LOAD_LANE])
        assert legs == [LaneShard(lane=UNDER_LOAD_LANE, index=1, total=1)]

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
    """The blocking-finding fix: NO emitted job exceeds the budget-safe bound.

    Run against the LIVE catalog (181 = clean_room 167 / under_load 14) so the
    test goes RED the moment the catalog grows past what the bound allows without
    the matrix re-sharding — exactly the silent budget-degrade #2492 warns about.
    """

    def test_no_emitted_shard_exceeds_the_budget_safe_bound(self) -> None:
        specs = discover_specs()
        legs = plan_lane_shards(specs, sorted(PERMITTED_LANES))
        for leg in legs:
            lane_specs = [spec for spec in specs if spec.lane == leg.lane]
            shard_specs = filter_specs_by_shard(lane_specs, leg.shard)
            assert len(shard_specs) <= MAX_SCENARIOS_PER_SHARD, (
                f"leg {leg.lane} {leg.shard} meters {len(shard_specs)} scenarios, over the "
                f"budget-safe bound {MAX_SCENARIOS_PER_SHARD}; re-shard the lane in lane_matrix.py."
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
        assert len(legs) > 1, "clean_room (the dominant ~167-scenario lane) must be sharded, not one leg."
