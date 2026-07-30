"""Reader for ``render_matrix_html``'s per-model tally — pinned against the writer."""

from pathlib import Path

import pytest

from teatree.eval.matrix import MatrixRow, render_matrix_html
from teatree.eval.matrix_html_tally import MatrixTallyError, ModelTally, parse_model_tallies
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


class TestParseModelTallies:
    def test_round_trips_the_rendered_tally(self) -> None:
        rows = [
            MatrixRow(scenario="alpha", model="opus", passed=True, score=1.0, trials=1, skipped=False, cost_usd=0.25),
            MatrixRow(scenario="beta", model="opus", passed=False, score=0.0, trials=1, skipped=False, cost_usd=0.2246),
            MatrixRow(scenario="alpha", model="haiku", passed=False, score=0.0, trials=1, skipped=False),
            MatrixRow(scenario="beta", model="haiku", passed=False, score=0.0, trials=1, skipped=True, errored=False),
        ]
        specs = [_spec("alpha"), _spec("beta")]

        tallies = parse_model_tallies(render_matrix_html(rows, ["opus", "haiku"], specs))

        assert tallies == [
            ModelTally(model="opus", passed=1, failed=1, skipped=0, errored=0, cost_usd=0.4746),
            ModelTally(model="haiku", passed=0, failed=1, skipped=1, errored=0, cost_usd=0.0),
        ]

    def test_escaped_model_name_is_unescaped(self) -> None:
        rows = [MatrixRow(scenario="alpha", model="m&1", passed=True, score=1.0, trials=1, skipped=False, cost_usd=0.5)]

        tallies = parse_model_tallies(render_matrix_html(rows, ["m&1"], [_spec("alpha")]))

        assert tallies[0].model == "m&1"

    def test_no_models_yields_no_tallies(self) -> None:
        assert parse_model_tallies(render_matrix_html([], [], [])) == []

    def test_document_without_a_tally_table_fails_loud(self) -> None:
        with pytest.raises(MatrixTallyError):
            parse_model_tallies("<!doctype html><html><body><table><tr><td>x</td></tr></table></body></html>")


class TestModelTally:
    def test_verdicts_exclude_skipped_and_errored(self) -> None:
        tally = ModelTally(model="opus", passed=2, failed=3, skipped=4, errored=5, cost_usd=0.0)

        assert tally.verdicts == 5

    def test_unmetered_needs_verdicts_and_zero_cost(self) -> None:
        assert ModelTally(model="a", passed=0, failed=3, skipped=0, errored=0, cost_usd=0.0).is_unmetered
        assert not ModelTally(model="a", passed=0, failed=3, skipped=0, errored=0, cost_usd=0.01).is_unmetered
        assert not ModelTally(model="a", passed=0, failed=0, skipped=3, errored=0, cost_usd=0.0).is_unmetered
