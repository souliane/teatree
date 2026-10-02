"""``scripts/eval/shard_matrix.py`` emits the eval-ci-heal shard matrix JSON.

The full suite (empty ``scenarios``) fans out ``--shards`` legs of ``i/N`` shard
tokens; a red-subset re-run (non-empty ``scenarios``) is NOT sharded — one leg
with an empty token runs the named subset loop. The helper imports nothing from
teatree, so the shard token slices the live catalog in-container at run time.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "shard_matrix",
    Path(__file__).parents[2] / "scripts" / "eval" / "shard_matrix.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

shard_matrix = _MOD.shard_matrix
main = _MOD.main


class TestShardMatrix:
    def test_full_suite_fans_out_n_shard_legs(self) -> None:
        legs = shard_matrix(8, "")
        assert legs == [{"shard": f"{i}/8"} for i in range(1, 9)]

    def test_subset_is_a_single_unsharded_leg(self) -> None:
        assert shard_matrix(8, "alpha,beta") == [{"shard": ""}]

    def test_whitespace_only_scenarios_still_shards_the_full_suite(self) -> None:
        assert shard_matrix(3, "   ") == [{"shard": "1/3"}, {"shard": "2/3"}, {"shard": "3/3"}]

    def test_zero_shards_fails_loud(self) -> None:
        with pytest.raises(SystemExit):
            shard_matrix(0, "")


class TestCli:
    def test_main_prints_the_matrix_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--shards", "6"])
        assert code == 0
        legs = json.loads(capsys.readouterr().out)
        assert len(legs) == 6
        assert legs[0] == {"shard": "1/6"}

    def test_main_subset_prints_one_leg(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--shards", "6", "--scenarios", "alpha"])
        assert code == 0
        assert json.loads(capsys.readouterr().out) == [{"shard": ""}]
