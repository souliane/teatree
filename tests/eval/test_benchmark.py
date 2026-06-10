"""Per-variant benchmark summary math and renderers (``t3 eval benchmark``)."""

import json

import pytest

from teatree.eval.benchmark import VariantSummary, render_benchmark_json, render_benchmark_text, summarize_benchmark
from teatree.eval.matrix import MatrixRow


def _row(
    scenario: str,
    model: str,
    *,
    passed: bool = True,
    skipped: bool = False,
    cost_usd: float = 0.0,
) -> MatrixRow:
    return MatrixRow(
        scenario=scenario,
        model=model,
        passed=passed,
        score=1.0 if passed else 0.0,
        trials=1,
        skipped=skipped,
        cost_usd=cost_usd,
    )


class TestSummarizeBenchmark:
    def test_per_variant_counts_and_costs(self) -> None:
        rows = [
            _row("alpha", "opus@xhigh", passed=True, cost_usd=0.10),
            _row("beta", "opus@xhigh", passed=False, cost_usd=0.30),
            _row("alpha", "fable@medium", passed=True, cost_usd=0.02),
            _row("beta", "fable@medium", passed=True, cost_usd=0.04),
        ]
        opus, fable = summarize_benchmark(rows, ["opus@xhigh", "fable@medium"])
        assert opus.variant == "opus@xhigh"
        assert (opus.passed, opus.executed, opus.skipped) == (1, 2, 0)
        assert opus.pass_rate == pytest.approx(0.5)
        assert opus.total_cost_usd == pytest.approx(0.40)
        assert opus.mean_cost_usd == pytest.approx(0.20)
        assert opus.cost_per_pass_usd == pytest.approx(0.40)
        assert fable.pass_rate == pytest.approx(1.0)
        assert fable.cost_per_pass_usd == pytest.approx(0.03)

    def test_summaries_follow_the_given_variant_order(self) -> None:
        rows = [_row("alpha", "b"), _row("alpha", "a")]
        assert [s.variant for s in summarize_benchmark(rows, ["a", "b"])] == ["a", "b"]

    def test_zero_passes_has_no_cost_per_pass(self) -> None:
        rows = [_row("alpha", "m", passed=False, cost_usd=0.10)]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.passed == 0
        assert summary.cost_per_pass_usd is None

    def test_skipped_rows_count_as_skipped_not_executed(self) -> None:
        rows = [
            _row("alpha", "m", passed=False, skipped=True),
            _row("beta", "m", passed=True, cost_usd=0.10),
        ]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert (summary.passed, summary.executed, summary.skipped) == (1, 1, 1)
        assert summary.pass_rate == pytest.approx(1.0)
        assert summary.mean_cost_usd == pytest.approx(0.10)

    def test_all_skipped_variant_has_zero_rates_not_a_crash(self) -> None:
        rows = [_row("alpha", "m", skipped=True)]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.executed == 0
        assert summary.pass_rate == pytest.approx(0.0)
        assert summary.mean_cost_usd == pytest.approx(0.0)
        assert summary.cost_per_pass_usd is None


class TestRenderBenchmarkText:
    def test_table_shows_each_variant_with_its_metrics(self) -> None:
        summaries = [
            VariantSummary(variant="opus@xhigh", passed=1, executed=2, skipped=0, total_cost_usd=0.40),
            VariantSummary(variant="fable@medium", passed=2, executed=2, skipped=0, total_cost_usd=0.06),
        ]
        text = render_benchmark_text(summaries)
        assert "opus@xhigh" in text
        assert "fable@medium" in text
        assert "1/2" in text
        assert "2/2" in text
        assert "$0.4000" in text
        assert "$0.0300" in text

    def test_zero_pass_variant_renders_a_dash_for_cost_per_pass(self) -> None:
        summaries = [VariantSummary(variant="m", passed=0, executed=1, skipped=0, total_cost_usd=0.10)]
        text = render_benchmark_text(summaries)
        assert "-" in text

    def test_no_summaries_renders_just_the_header(self) -> None:
        header, separator = render_benchmark_text([]).splitlines()
        assert header.startswith("variant")
        assert set(separator) == {"-"}


class TestRenderBenchmarkJson:
    def test_json_shape(self) -> None:
        summaries = [
            VariantSummary(variant="opus@xhigh", passed=1, executed=2, skipped=0, total_cost_usd=0.40),
            VariantSummary(variant="m", passed=0, executed=1, skipped=1, total_cost_usd=0.10),
        ]
        payload = json.loads(render_benchmark_json(summaries))
        assert [v["variant"] for v in payload["variants"]] == ["opus@xhigh", "m"]
        first, second = payload["variants"]
        assert first == {
            "variant": "opus@xhigh",
            "passed": 1,
            "executed": 2,
            "skipped": 0,
            "pass_rate": 0.5,
            "total_cost_usd": 0.40,
            "mean_cost_usd": 0.20,
            "cost_per_pass_usd": 0.40,
        }
        assert second["cost_per_pass_usd"] is None
