"""Per-variant benchmark summary math and renderers (``t3 eval benchmark``)."""

import json

import pytest

from teatree.eval.benchmark import VariantSummary, render_benchmark_json, render_benchmark_text, summarize_benchmark
from teatree.eval.matrix import MatrixRow
from teatree.eval.models import TokenUsage


def _row(  # noqa: PLR0913 — test-data builder: each kwarg maps 1:1 to a MatrixRow field a case varies.
    scenario: str,
    model: str,
    *,
    passed: bool = True,
    skipped: bool = False,
    cost_usd: float = 0.0,
    errored: bool = False,
    usage: TokenUsage | None = None,
    fell_back: bool = False,
    terminal_reason: str = "",
) -> MatrixRow:
    return MatrixRow(
        scenario=scenario,
        model=model,
        passed=passed,
        score=1.0 if passed else 0.0,
        trials=1,
        skipped=skipped,
        cost_usd=cost_usd,
        errored=errored,
        usage=usage if usage is not None else TokenUsage(),
        fell_back=fell_back,
        terminal_reason=terminal_reason,
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

    def test_errored_rows_counted_as_errored_and_excluded_from_executed(self) -> None:
        rows = [
            _row("alpha", "m", passed=False, errored=True),
            _row("beta", "m", passed=True, cost_usd=0.10),
        ]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert (summary.passed, summary.executed, summary.skipped, summary.errored) == (1, 1, 0, 1)
        assert summary.pass_rate == pytest.approx(1.0)
        assert summary.mean_cost_usd == pytest.approx(0.10)

    def test_errored_cell_cost_excluded_from_total(self) -> None:
        rows = [
            _row("alpha", "m", passed=False, errored=True, cost_usd=0.30),
            _row("beta", "m", passed=True, cost_usd=0.10),
        ]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.total_cost_usd == pytest.approx(0.10)


class TestUsageAggregation:
    def test_usage_summed_token_weighted_across_executed_cells(self) -> None:
        rows = [
            _row("a", "m", cost_usd=0.1, usage=TokenUsage(input=100, cache_creation=200, cache_read=700, output=50)),
            _row("b", "m", cost_usd=0.1, usage=TokenUsage(input=50, cache_creation=50, cache_read=900, output=30)),
        ]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.usage == TokenUsage(input=150, cache_creation=250, cache_read=1600, output=80)
        # token-weighted (sum-then-divide), NOT a mean of per-cell ratios.
        assert summary.cache_hit_rate == pytest.approx(1600 / 2000)
        assert summary.cold_write_fraction == pytest.approx(250 / 2000)
        assert summary.mean_output_tokens == pytest.approx(80 / 2)

    def test_errored_cells_excluded_from_usage_aggregation(self) -> None:
        rows = [
            _row("a", "m", cost_usd=0.1, usage=TokenUsage(input=100, cache_read=900, output=40)),
            _row("b", "m", errored=True, passed=False, usage=TokenUsage(input=9999, cache_read=9999, output=9999)),
        ]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.usage == TokenUsage(input=100, cache_read=900, output=40)
        assert summary.cache_hit_rate == pytest.approx(0.9)

    def test_zero_input_has_zero_fractions_not_a_crash(self) -> None:
        (summary,) = summarize_benchmark([_row("a", "m", skipped=True)], ["m"])
        assert summary.cache_hit_rate == pytest.approx(0.0)
        assert summary.cold_write_fraction == pytest.approx(0.0)
        assert summary.mean_output_tokens == pytest.approx(0.0)

    def test_fell_back_cells_counted(self) -> None:
        rows = [
            _row("a", "m", cost_usd=0.1, usage=TokenUsage(input=10), fell_back=True),
            _row("b", "m", cost_usd=0.1, usage=TokenUsage(input=10)),
        ]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.fell_back_cells == 1


class TestWarmEquivalent:
    def _clean_rows(self) -> list[MatrixRow]:
        base_in, out = 3e-6, 1.5e-5
        specs = [
            (1000, 2000, 5000, 300),
            (800, 600, 200, 1200),
            (1500, 1000, 9000, 50),
            (400, 900, 100, 2000),
        ]
        rows = []
        for i, (inp, cc, cr, o) in enumerate(specs):
            usage = TokenUsage(input=inp, cache_creation=cc, cache_read=cr, output=o)
            billed = base_in * usage.effective_billed_input + out * usage.output
            rows.append(_row(f"s{i}", "m", cost_usd=billed, usage=usage))
        return rows

    def test_warm_equivalent_below_total_on_a_well_conditioned_suite(self) -> None:
        rows = self._clean_rows()
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.warm_equivalent_cost_usd is not None
        assert summary.warm_equivalent_cost_usd < summary.total_cost_usd

    def test_warm_equivalent_is_none_on_too_few_clean_cells(self) -> None:
        rows = self._clean_rows()[:2]
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.warm_equivalent_cost_usd is None

    def test_fallback_and_capped_and_zero_cost_cells_excluded_from_the_fit(self) -> None:
        # 4 clean cells + a fallback + a zero-cost cell: the fit uses only the 4
        # clean cells, so a known-rates suite still recovers a warm-equivalent.
        rows = self._clean_rows()
        rows.append(_row("fb", "m", cost_usd=0.5, usage=TokenUsage(input=100, output=50), fell_back=True))
        rows.append(_row("zero", "m", cost_usd=0.0, usage=TokenUsage(input=100, output=50)))
        (summary,) = summarize_benchmark(rows, ["m"])
        assert summary.warm_equivalent_cost_usd is not None

    def test_capped_cell_is_excluded_from_the_fit(self) -> None:
        # The 4 clean cells alone recover a warm-equivalent. Add a 5th cell that
        # is metered (cost_usd > 0) and NOT fell-back, but paid a cap cost wildly
        # off the clean billed identity (a cap-truncated run keeps its tokens but
        # is billed a partial/aborted amount). It carries a cap terminal_reason,
        # so `_clean_cost_cells` must EXCLUDE it from BOTH the fit and the
        # warm-equivalent sum — leaving the value byte-for-byte the clean-4 fit.
        # Deleting the `terminal_reason not in CAP_TERMINAL_REASONS` branch in
        # `_clean_cost_cells` lets this cell into the regressors and changes the
        # recovered rates + the total, so this assertion goes RED (anti-vacuous).
        clean = self._clean_rows()
        (clean_only,) = summarize_benchmark(clean, ["m"])
        biasing_usage = TokenUsage(input=2000, cache_creation=2000, cache_read=2000, output=2000)
        capped = _row("capped", "m", cost_usd=9.99, usage=biasing_usage, terminal_reason="budget_exceeded")
        (with_capped,) = summarize_benchmark([*clean, capped], ["m"])
        assert clean_only.warm_equivalent_cost_usd is not None
        assert with_capped.warm_equivalent_cost_usd is not None
        assert with_capped.warm_equivalent_cost_usd == pytest.approx(clean_only.warm_equivalent_cost_usd)


class TestRenderBenchmarkText:
    def test_table_shows_each_variant_with_its_metrics(self) -> None:
        summaries = [
            VariantSummary(variant="opus@xhigh", passed=1, executed=2, skipped=0, errored=0, total_cost_usd=0.40),
            VariantSummary(variant="fable@medium", passed=2, executed=2, skipped=0, errored=0, total_cost_usd=0.06),
        ]
        text = render_benchmark_text(summaries)
        assert "opus@xhigh" in text
        assert "fable@medium" in text
        assert "1/2" in text
        assert "2/2" in text
        assert "$0.4000" in text
        assert "$0.0300" in text

    def test_errored_column_present(self) -> None:
        summaries = [VariantSummary(variant="m", passed=1, executed=1, skipped=0, errored=2, total_cost_usd=0.10)]
        text = render_benchmark_text(summaries)
        header = text.splitlines()[0]
        assert "errored" in header
        assert "2" in text

    def test_zero_pass_variant_renders_a_dash_for_cost_per_pass(self) -> None:
        summaries = [VariantSummary(variant="m", passed=0, executed=1, skipped=0, errored=0, total_cost_usd=0.10)]
        text = render_benchmark_text(summaries)
        assert "-" in text

    def test_no_summaries_renders_just_the_header(self) -> None:
        header, separator = render_benchmark_text([]).splitlines()
        assert header.startswith("variant")
        assert set(separator) == {"-"}
        assert "errored" in header

    def test_new_cache_cost_columns_present(self) -> None:
        summaries = [
            VariantSummary(
                variant="m",
                passed=1,
                executed=2,
                skipped=0,
                errored=0,
                total_cost_usd=0.40,
                usage=TokenUsage(input=100, cache_creation=200, cache_read=700, output=120),
                warm_equivalent_cost_usd=0.05,
            )
        ]
        text = render_benchmark_text(summaries)
        header = text.splitlines()[0]
        assert "cache-hit%" in header
        assert "cold-write%" in header
        assert "mean-out-tok" in header
        assert "warm-cost" in header

    def test_warm_cost_renders_dash_when_none(self) -> None:
        summaries = [
            VariantSummary(
                variant="m",
                passed=1,
                executed=1,
                skipped=0,
                errored=0,
                total_cost_usd=0.10,
                warm_equivalent_cost_usd=None,
            )
        ]
        text = render_benchmark_text(summaries)
        # the warm-cost cell is a dash, not a fabricated number.
        assert "$0.05" not in text

    def test_fallback_note_rendered_when_a_cell_fell_back(self) -> None:
        summaries = [
            VariantSummary(
                variant="opus@xhigh", passed=1, executed=2, skipped=0, errored=0, total_cost_usd=0.40, fell_back_cells=2
            )
        ]
        text = render_benchmark_text(summaries)
        assert "fell back" in text
        assert "opus@xhigh" in text.splitlines()[-1]

    def test_no_fallback_note_when_no_cell_fell_back(self) -> None:
        summaries = [VariantSummary(variant="m", passed=1, executed=1, skipped=0, errored=0, total_cost_usd=0.10)]
        assert "fell back" not in render_benchmark_text(summaries)


class TestRenderBenchmarkJson:
    def test_json_shape(self) -> None:
        summaries = [
            VariantSummary(
                variant="opus@xhigh",
                passed=1,
                executed=2,
                skipped=0,
                errored=0,
                total_cost_usd=0.40,
                usage=TokenUsage(input=100, cache_creation=200, cache_read=700, output=120),
                fell_back_cells=0,
                warm_equivalent_cost_usd=0.05,
            ),
            VariantSummary(variant="m", passed=0, executed=1, skipped=1, errored=3, total_cost_usd=0.10),
        ]
        payload = json.loads(render_benchmark_json(summaries))
        assert [v["variant"] for v in payload["variants"]] == ["opus@xhigh", "m"]
        first, second = payload["variants"]
        assert first == {
            "variant": "opus@xhigh",
            "passed": 1,
            "executed": 2,
            "skipped": 0,
            "errored": 0,
            "pass_rate": 0.5,
            "total_cost_usd": 0.40,
            "mean_cost_usd": 0.20,
            "cost_per_pass_usd": 0.40,
            "usage": {"input": 100, "cache_creation": 200, "cache_read": 700, "output": 120},
            "cache_hit_rate": pytest.approx(0.7),
            "cold_write_fraction": pytest.approx(0.2),
            "mean_output_tokens": pytest.approx(60.0),
            "warm_equivalent_cost_usd": 0.05,
            "fell_back_cells": 0,
        }
        assert second["errored"] == 3
        assert second["cost_per_pass_usd"] is None
        assert second["warm_equivalent_cost_usd"] is None
