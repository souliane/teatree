"""The metered eval fans out across SHARDS so each leg fits the budget (#2492).

``lane_matrix.py`` emits the per-leg ``{lane, shard}`` list a GitHub Actions
``strategy.matrix`` consumes via ``fromJSON`` (as ``include``). Empty lane →
every permitted lane, each sharded into budget-safe legs; an explicit lane →
that single lane, sharded; an unknown lane fails loud. The catalog is read live,
so the split is never stale.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from teatree.eval.discovery import discover_specs
from teatree.eval.lane_shard import filter_specs_by_shard, max_scenarios_per_shard, shard_count_for
from teatree.eval.models import CLEAN_ROOM_LANE, PERMITTED_LANES, UNDER_LOAD_LANE

_SPEC = importlib.util.spec_from_file_location(
    "lane_matrix",
    Path(__file__).parents[2] / "scripts" / "eval" / "lane_matrix.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

_lanes_for = _MOD._lanes_for
_matrix_for = _MOD._matrix_for
_efforts_for = _MOD._efforts_for
_shard_indices_for = _MOD._shard_indices_for
main = _MOD.main


class TestLanesFor:
    def test_empty_lane_fans_out_to_every_permitted_lane(self) -> None:
        assert _lanes_for("") == sorted(PERMITTED_LANES)

    def test_whitespace_lane_is_treated_as_empty(self) -> None:
        assert _lanes_for("   ") == sorted(PERMITTED_LANES)

    def test_explicit_lane_runs_only_that_lane(self) -> None:
        assert _lanes_for("under_load") == ["under_load"]

    def test_every_permitted_lane_resolves_to_a_single_leg(self) -> None:
        for lane in PERMITTED_LANES:
            assert _lanes_for(lane) == [lane]

    def test_unknown_lane_fails_loud_with_the_permitted_set(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _lanes_for("nonexistent_lane")
        assert "nonexistent_lane" in str(exc.value)
        for lane in PERMITTED_LANES:
            assert lane in str(exc.value)


class TestMatrixFor:
    def test_each_entry_is_a_lane_shard_object(self) -> None:
        for entry in _matrix_for(""):
            assert set(entry) == {"lane", "shard"}
            assert entry["lane"] in PERMITTED_LANES
            index, total = entry["shard"].split("/")
            assert 1 <= int(index) <= int(total)

    def test_under_load_shards_match_the_live_catalog(self) -> None:
        count = sum(1 for spec in discover_specs() if spec.lane == UNDER_LOAD_LANE)
        total = shard_count_for(count, UNDER_LOAD_LANE)
        expected = [{"lane": UNDER_LOAD_LANE, "shard": f"{index}/{total}"} for index in range(1, total + 1)]
        under = [e for e in _matrix_for("") if e["lane"] == UNDER_LOAD_LANE]
        assert under == expected

    def test_under_load_is_split_into_multiple_shards(self) -> None:
        # #2683: under_load's roster-spawning scenarios are 10-45 min each, so the
        # lane must split into multiple shards, never run as one 1/1 leg.
        under = [e for e in _matrix_for("") if e["lane"] == UNDER_LOAD_LANE]
        assert len(under) > 1, "under_load must be sharded (roster-spawning scenarios), not one 1/1 leg."

    def test_clean_room_is_split_into_multiple_shards(self) -> None:
        clean = [e for e in _matrix_for("") if e["lane"] == CLEAN_ROOM_LANE]
        assert len(clean) > 1, "the dominant clean_room lane must be sharded, not one giant leg."

    def test_explicit_lane_shards_only_that_lane(self) -> None:
        entries = _matrix_for("under_load")
        assert {e["lane"] for e in entries} == {UNDER_LOAD_LANE}

    def test_every_emitted_leg_meters_a_budget_safe_subset(self) -> None:
        specs = discover_specs()
        for entry in _matrix_for(""):
            lane_specs = [s for s in specs if s.lane == entry["lane"]]
            shard_specs = filter_specs_by_shard(lane_specs, entry["shard"])
            assert len(shard_specs) <= max_scenarios_per_shard(entry["lane"])


class TestEffortsFor:
    def test_empty_efforts_is_the_no_axis_sentinel(self) -> None:
        # No --efforts → a single None tier, so the matrix keeps the legacy
        # {lane, shard} shape (no effort axis, no behaviour change).
        assert _efforts_for("") == [None]

    def test_explicit_three_tier_matrix(self) -> None:
        assert _efforts_for("low,medium,high") == ["low", "medium", "high"]

    def test_blank_entries_dropped(self) -> None:
        assert _efforts_for("low,,high") == ["low", "high"]

    def test_unknown_effort_fails_loud(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _efforts_for("low,bogus")
        assert "bogus" in str(exc.value)


class TestMatrixForWithEfforts:
    def test_no_effort_axis_keeps_the_legacy_lane_shard_shape(self) -> None:
        for entry in _matrix_for("", efforts=""):
            assert set(entry) == {"lane", "shard"}

    def test_three_tier_axis_multiplies_each_leg_across_efforts(self) -> None:
        base = _matrix_for("under_load", efforts="")
        tiered = _matrix_for("under_load", efforts="low,medium,high")
        # Each {lane, shard} leg appears once per effort tier.
        assert len(tiered) == len(base) * 3
        for entry in tiered:
            assert set(entry) == {"lane", "shard", "effort"}
            assert entry["effort"] in {"low", "medium", "high"}
        # Every (lane, shard) base leg is present at all three tiers.
        for leg in base:
            for tier in ("low", "medium", "high"):
                assert {**leg, "effort": tier} in tiered

    def test_every_tiered_leg_is_budget_safe(self) -> None:
        specs = discover_specs()
        for entry in _matrix_for("", efforts="low,medium,high"):
            lane_specs = [s for s in specs if s.lane == entry["lane"]]
            shard_specs = filter_specs_by_shard(lane_specs, entry["shard"])
            assert len(shard_specs) <= max_scenarios_per_shard(entry["lane"])


class TestShardIndicesFor:
    def test_empty_is_no_filter(self) -> None:
        assert _shard_indices_for("") is None

    def test_whitespace_is_no_filter(self) -> None:
        assert _shard_indices_for("   ") is None

    def test_comma_list(self) -> None:
        assert _shard_indices_for("1,3,7") == {1, 3, 7}

    def test_range(self) -> None:
        assert _shard_indices_for("1-6") == {1, 2, 3, 4, 5, 6}

    def test_range_and_comma_list_combine(self) -> None:
        assert _shard_indices_for("1-3,7") == {1, 2, 3, 7}

    def test_blank_entries_between_commas_are_dropped(self) -> None:
        assert _shard_indices_for("1,,3") == {1, 3}

    def test_non_numeric_token_fails_loud(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _shard_indices_for("a")
        assert "a" in str(exc.value)

    def test_reversed_range_fails_loud(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _shard_indices_for("6-1")
        assert "6-1" in str(exc.value)

    def test_zero_index_fails_loud(self) -> None:
        with pytest.raises(SystemExit):
            _shard_indices_for("0")

    def test_malformed_range_fails_loud(self) -> None:
        with pytest.raises(SystemExit):
            _shard_indices_for("1-2-3")


class TestMatrixForWithShards:
    def test_empty_shards_is_unfiltered(self) -> None:
        assert _matrix_for("", shards="") == _matrix_for("")

    def test_range_filters_indices_within_each_lane(self) -> None:
        entries = _matrix_for("", shards="1-6")
        clean = [e for e in entries if e["lane"] == CLEAN_ROOM_LANE]
        under = [e for e in entries if e["lane"] == UNDER_LOAD_LANE]
        # under_load has fewer than 6 shards live — every shard of it survives
        # the filter unchanged, while clean_room (which has more) is truncated.
        under_all = list(_matrix_for("under_load"))
        assert under == under_all
        assert len(clean) == 6
        for entry in clean:
            index, _total = entry["shard"].split("/")
            assert int(index) <= 6

    def test_comma_list_filters_to_exactly_those_indices(self) -> None:
        entries = _matrix_for("clean_room", shards="1,3")
        indices = sorted(int(e["shard"].split("/")[0]) for e in entries)
        assert indices == [1, 3]

    def test_index_beyond_every_lane_shard_count_is_an_explicit_error(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _matrix_for("", shards="99")
        assert "99" in str(exc.value)

    def test_malformed_non_numeric_shard_fails_loud(self) -> None:
        with pytest.raises(SystemExit):
            _matrix_for("", shards="a")

    def test_reversed_shard_range_fails_loud(self) -> None:
        with pytest.raises(SystemExit):
            _matrix_for("", shards="6-1")

    def test_shards_and_efforts_compose(self) -> None:
        entries = _matrix_for("clean_room", efforts="low,medium", shards="1,2")
        indices = {int(e["shard"].split("/")[0]) for e in entries}
        assert indices == {1, 2}
        assert {e["effort"] for e in entries} == {"low", "medium"}


class TestMain:
    def test_empty_lane_prints_every_leg_as_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main([])
        assert code == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed == _matrix_for("")
        assert all(set(entry) == {"lane", "shard"} for entry in printed)

    def test_explicit_lane_prints_that_lanes_shards(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--lane", "clean_room"])
        assert code == 0
        printed = json.loads(capsys.readouterr().out)
        assert {e["lane"] for e in printed} == {CLEAN_ROOM_LANE}

    def test_efforts_flag_emits_the_effort_axis(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--lane", "under_load", "--efforts", "low,medium,high"])
        assert code == 0
        printed = json.loads(capsys.readouterr().out)
        assert all(set(entry) == {"lane", "shard", "effort"} for entry in printed)
        assert {e["effort"] for e in printed} == {"low", "medium", "high"}

    def test_unknown_lane_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(["--lane", "bogus"])

    def test_unknown_effort_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(["--efforts", "low,bogus"])

    def test_shards_flag_filters_the_printed_matrix(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--lane", "clean_room", "--shards", "1,2"])
        assert code == 0
        printed = json.loads(capsys.readouterr().out)
        indices = {int(e["shard"].split("/")[0]) for e in printed}
        assert indices == {1, 2}

    def test_shards_flag_malformed_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(["--shards", "6-1"])
