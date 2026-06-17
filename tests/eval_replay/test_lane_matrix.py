"""The metered eval fans out across lanes so each leg fits the budget (#2492).

``lane_matrix.py`` emits the per-leg lane list a GitHub Actions
``strategy.matrix`` consumes via ``fromJSON``. Empty lane → every permitted lane
(one leg each, full coverage in parallel); an explicit lane → that single lane;
an unknown lane fails loud.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from teatree.eval.models import PERMITTED_LANES

_SPEC = importlib.util.spec_from_file_location(
    "lane_matrix",
    Path(__file__).parents[2] / "scripts" / "eval" / "lane_matrix.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

_lanes_for = _MOD._lanes_for
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


class TestMain:
    def test_empty_lane_prints_every_lane_as_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main([])
        assert code == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed == sorted(PERMITTED_LANES)

    def test_explicit_lane_prints_a_single_element_array(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--lane", "clean_room"])
        assert code == 0
        assert json.loads(capsys.readouterr().out) == ["clean_room"]

    def test_unknown_lane_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(["--lane", "bogus"])
