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
from teatree.eval.lane_shard import MAX_SCENARIOS_PER_SHARD, filter_specs_by_shard
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

    def test_under_load_is_a_single_shard(self) -> None:
        under = [e for e in _matrix_for("") if e["lane"] == UNDER_LOAD_LANE]
        assert under == [{"lane": UNDER_LOAD_LANE, "shard": "1/1"}]

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
            assert len(shard_specs) <= MAX_SCENARIOS_PER_SHARD


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

    def test_unknown_lane_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(["--lane", "bogus"])
