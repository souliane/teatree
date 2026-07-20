"""``t3 eval set-baseline`` — regenerate the ``baseline`` preset from a matrix run.

End-to-end through the typer CLI + the real YAML/JSON loaders (``tmp_path``
files); only ``discover_specs`` is stubbed, so the "still discovered" /
"pruned" behavior is exercised for real.
"""

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from teatree.agents.model_tiering import TIER_MODELS
from teatree.cli import app
from teatree.eval.models import EvalSpec

_HAIKU = TIER_MODELS["cheap"]
_SONNET = TIER_MODELS["balanced"]
_OPUS = TIER_MODELS["frontier"]


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name, scenario="sc", agent_path="skills/code/SKILL.md", prompt="p", matchers=(), source_path=Path("x.yaml")
    )


def _cell(*, passed: bool, skipped: bool = False, errored: bool = False) -> dict[str, object]:
    return {"passed": passed, "skipped": skipped, "errored": errored, "score": 1.0 if passed else 0.0, "trials": 1}


def _write_matrix(path: Path, scenarios: dict[str, dict[str, dict[str, object] | None]]) -> None:
    payload = {
        "models": [_HAIKU, _SONNET, _OPUS],
        "scenarios": [{"name": name, "results": results} for name, results in scenarios.items()],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _invoke(args: list[str], *, discovered: list[EvalSpec]) -> object:
    with patch("teatree.cli.eval.set_baseline.discover_specs", return_value=discovered):
        return CliRunner().invoke(app, ["eval", "set-baseline", *args])


class TestCheapestPassingTier:
    def test_picks_the_cheapest_of_multiple_passing_tiers(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=True), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "balanced"}

    def test_a_cell_that_only_passes_cheap_picks_cheap(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=True), _SONNET: _cell(passed=True), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap"}

    def test_skipped_and_errored_cells_are_never_picked(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {
                    _HAIKU: _cell(passed=True, skipped=True),
                    _SONNET: _cell(passed=True, errored=True),
                    _OPUS: _cell(passed=True),
                }
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out), "--allow-frontier"], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "frontier"}


class TestPruning:
    def test_scenario_no_longer_discovered_is_pruned(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {_HAIKU: _cell(passed=True)},
                "renamed_away": {_HAIKU: _cell(passed=True)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap"}
        assert "renamed_away" not in written["scenarios"]


class TestStableSort:
    def test_output_keys_are_sorted_regardless_of_input_order(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "zeta": {_HAIKU: _cell(passed=True)},
                "alpha": {_HAIKU: _cell(passed=True)},
                "mu": {_HAIKU: _cell(passed=True)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(
            ["--from", str(matrix), "--out", str(out)],
            discovered=[_spec("zeta"), _spec("alpha"), _spec("mu")],
        )
        assert result.exit_code == 0, result.output
        raw = out.read_text(encoding="utf-8")
        scenarios_block = raw.split("scenarios:")[1].split("frontier_ok:")[0]
        names_in_order = [line.strip().rstrip(":").rstrip() for line in scenarios_block.splitlines() if line.strip()]
        assert [n.split(":")[0] for n in names_in_order] == ["alpha", "mu", "zeta"]


class TestFrontierRefusal:
    def test_frontier_only_pass_is_refused_without_allow_frontier(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 2
        assert "--allow-frontier" in result.output
        assert not out.exists()

    def test_allow_frontier_writes_the_entry_and_frontier_ok(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {"alpha": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=True)}},
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out), "--allow-frontier"], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "frontier"}
        assert written["frontier_ok"] == ["alpha"]


class TestFailedEverywhere:
    def test_scenario_failing_every_tier_gets_no_entry_and_a_warning(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(
            matrix,
            {
                "alpha": {_HAIKU: _cell(passed=True)},
                "beta": {_HAIKU: _cell(passed=False), _SONNET: _cell(passed=False), _OPUS: _cell(passed=False)},
            },
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha"), _spec("beta")])
        assert result.exit_code == 0, result.output
        assert "WARNING beta" in result.output
        written = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert written["scenarios"] == {"alpha": "cheap"}
        assert "beta" not in written["scenarios"]


class TestHeaderAndUnknownColumn:
    def test_output_carries_the_generated_header(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        _write_matrix(matrix, {"alpha": {_HAIKU: _cell(passed=True)}})
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 0, result.output
        assert out.read_text(encoding="utf-8").startswith("# GENERATED by t3 eval set-baseline")

    def test_unrecognized_matrix_column_is_fail_loud(self, tmp_path: Path) -> None:
        matrix = tmp_path / "matrix.json"
        matrix.write_text(
            json.dumps(
                {
                    "models": ["some-custom-model"],
                    "scenarios": [{"name": "alpha", "results": {"some-custom-model": _cell(passed=True)}}],
                }
            ),
            encoding="utf-8",
        )
        out = tmp_path / "baseline.yaml"
        result = _invoke(["--from", str(matrix), "--out", str(out)], discovered=[_spec("alpha")])
        assert result.exit_code == 2
        assert "some-custom-model" in result.output
