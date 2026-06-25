"""Model-regression matrix renderers (#1160)."""

import json
from pathlib import Path

from teatree.eval.matrix import MatrixRow, matrix_cell, render_matrix_html, render_matrix_json, render_matrix_text
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


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _row(  # noqa: PLR0913 — test-data builder: each kwarg maps 1:1 to a MatrixRow field a case varies.
    scenario: str,
    model: str,
    *,
    passed: bool = True,
    skipped: bool = False,
    trials: int = 1,
    errored: bool = False,
) -> MatrixRow:
    return MatrixRow(
        scenario=scenario,
        model=model,
        passed=passed,
        score=1.0 if passed else 0.0,
        trials=trials,
        skipped=skipped,
        errored=errored,
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

    def test_errored_is_err(self) -> None:
        assert matrix_cell(_row("a", "haiku", passed=False, errored=True)) == "ERR"


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

    def test_errored_cell_excluded_from_failed_and_counted_as_errored(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        rows = [
            _row("alpha", "opus"),
            _row("beta", "opus", passed=False, errored=True),
        ]
        text = render_matrix_text(rows, ["opus"], specs)
        assert "opus: 1 passed, 0 failed, 0 skipped, 1 errored" in text
        assert "ERR" in text


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

    def test_errored_cell_carries_errored_flag(self) -> None:
        specs = [_spec("alpha")]
        rows = [_row("alpha", "opus", passed=False, errored=True)]
        payload = json.loads(render_matrix_json(rows, ["opus"], specs))
        cell = payload["scenarios"][0]["results"]["opus"]
        assert cell["errored"] is True
        assert cell["passed"] is False


class TestRenderMatrixHtml:
    def test_self_contained_table_with_model_columns(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        rows = [_row("alpha", "m-one"), _row("beta", "m-two", passed=False)]
        html = render_matrix_html(rows, ["m-one", "m-two"], specs)
        assert html.startswith("<!doctype html>")
        # Each model is a column header; each scenario a row.
        assert "<th>m-one</th>" in html
        assert "<th>m-two</th>" in html
        assert "alpha" in html
        assert "beta" in html

    def test_cells_colour_coded_by_verdict(self) -> None:
        specs = [_spec("alpha")]
        rows = [_row("alpha", "m-one", passed=True), _row("alpha", "m-two", passed=False)]
        html = render_matrix_html(rows, ["m-one", "m-two"], specs)
        assert "class='pass'" in html
        assert "class='fail'" in html

    def test_errored_and_skip_cells(self) -> None:
        specs = [_spec("alpha")]
        rows = [_row("alpha", "m-one", errored=True), _row("alpha", "m-two", skipped=True)]
        html = render_matrix_html(rows, ["m-one", "m-two"], specs)
        assert "class='err'>ERR" in html
        assert "class='skip'>skip" in html

    def test_html_escapes_scenario_and_model_names(self) -> None:
        specs = [_spec("a<script>")]
        rows = [_row("a<script>", "m&1")]
        html = render_matrix_html(rows, ["m&1"], specs)
        assert "<script>" not in html.replace("&lt;script&gt;", "")
        assert "&lt;script&gt;" in html
        assert "m&amp;1" in html
