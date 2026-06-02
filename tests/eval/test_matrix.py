"""Model-regression matrix renderers (#1160)."""

import json
from pathlib import Path

from teatree.eval.matrix import MatrixRow, matrix_cell, render_matrix_json, render_matrix_text
from teatree.eval.models import EvalSpec


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        judge=None,
    )


def _row(scenario: str, model: str, *, passed: bool = True, skipped: bool = False, trials: int = 1) -> MatrixRow:
    return MatrixRow(
        scenario=scenario,
        model=model,
        passed=passed,
        score=1.0 if passed else 0.0,
        trials=trials,
        skipped=skipped,
    )


class TestMatrixCell:
    def test_missing_is_dash(self) -> None:
        assert matrix_cell(None) == "-"

    def test_skip(self) -> None:
        assert matrix_cell(_row("a", "haiku", skipped=True)) == "skip"

    def test_pass_and_fail(self) -> None:
        assert matrix_cell(_row("a", "haiku", passed=True)) == "pass"
        assert matrix_cell(_row("a", "haiku", passed=False)) == "FAIL"

    def test_multitrial_shows_rate(self) -> None:
        cell = matrix_cell(MatrixRow(scenario="a", model="haiku", passed=True, score=0.67, trials=3, skipped=False))
        assert cell == "0.67"


class TestRenderMatrixText:
    def test_table_has_models_scenarios_and_tally(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        rows = [
            _row("alpha", "opus"),
            _row("beta", "opus", passed=False),
            _row("alpha", "haiku"),
            _row("beta", "haiku"),
        ]
        text = render_matrix_text(rows, ["opus", "haiku"], specs)
        assert "opus" in text
        assert "haiku" in text
        assert "alpha" in text
        assert "beta" in text
        assert "opus: 1 passed, 1 failed, 0 skipped" in text
        assert "haiku: 2 passed, 0 failed, 0 skipped" in text


class TestRenderMatrixJson:
    def test_shape(self) -> None:
        specs = [_spec("alpha")]
        rows = [_row("alpha", "opus"), _row("alpha", "haiku", passed=False)]
        payload = json.loads(render_matrix_json(rows, ["opus", "haiku"], specs))
        assert payload["models"] == ["opus", "haiku"]
        results = payload["scenarios"][0]["results"]
        assert results["opus"]["passed"] is True
        assert results["haiku"]["passed"] is False

    def test_missing_cell_is_null(self) -> None:
        specs = [_spec("alpha")]
        rows = [_row("alpha", "opus")]
        payload = json.loads(render_matrix_json(rows, ["opus", "haiku"], specs))
        assert payload["scenarios"][0]["results"]["haiku"] is None
