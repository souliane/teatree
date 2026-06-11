"""Price math, cache multipliers, cycle boundaries, and report rendering."""

from datetime import date

import pytest

from teatree.core.cost import (
    DEFAULT_MONTHLY_CREDIT_USD,
    PRICE_TABLE,
    AttemptUsage,
    CostBreakdown,
    CostReport,
    ModelPrice,
    attempt_cost_usd,
    cycle_start,
    price_for_model,
    price_table_cost_usd,
    project_month_end_usd,
    tier_of_model,
    tier_rank,
)


class TestModelPrice:
    def test_input_output_priced_per_mtok(self) -> None:
        price = ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0)
        # 1M input + 1M output = $5 + $25.
        assert price.cost(input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(30.0)

    def test_cache_read_is_tenth_of_input(self) -> None:
        price = ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0)
        assert price.cache_read_per_mtok == pytest.approx(0.5)
        assert price.cost(cache_read_tokens=1_000_000) == pytest.approx(0.5)

    def test_cache_write_is_one_and_quarter_of_input(self) -> None:
        price = ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0)
        assert price.cache_write_per_mtok == pytest.approx(6.25)
        assert price.cost(cache_write_tokens=1_000_000) == pytest.approx(6.25)


class TestTierResolution:
    def test_short_names_map_to_tiers(self) -> None:
        assert price_for_model("opus").input_per_mtok == pytest.approx(5.0)
        assert price_for_model("sonnet").input_per_mtok == pytest.approx(3.0)
        assert price_for_model("haiku").input_per_mtok == pytest.approx(1.0)

    def test_dated_cli_model_id_maps_to_tier(self) -> None:
        assert tier_of_model("claude-opus-4-8[1m]") == "opus"
        assert tier_of_model("claude-sonnet-4-6") == "sonnet"
        assert tier_of_model("claude-haiku-4-5") == "haiku"

    def test_unknown_and_none_fall_back_to_reasoning_tier(self) -> None:
        assert tier_of_model(None) == "opus"
        assert tier_of_model("some-future-model") == "opus"


class TestFableTier:
    def test_fable_in_price_table_at_ten_fifty(self) -> None:
        assert PRICE_TABLE["fable"] == ModelPrice(input_per_mtok=10.0, output_per_mtok=50.0)

    def test_tier_of_model_recognises_fable_substring(self) -> None:
        assert tier_of_model("fable") == "fable"
        assert tier_of_model("claude-fable-5[1m]") == "fable"

    def test_fable_priced_above_opus(self) -> None:
        assert price_for_model("claude-fable-5").input_per_mtok > price_for_model("opus").input_per_mtok


class TestTierRank:
    def test_capability_order_haiku_lt_sonnet_lt_opus_lt_fable(self) -> None:
        assert tier_rank("haiku") < tier_rank("sonnet") < tier_rank("opus") < tier_rank("fable")

    def test_dated_ids_rank_like_their_tier(self) -> None:
        assert tier_rank("claude-fable-5[1m]") == tier_rank("fable")
        assert tier_rank("claude-sonnet-4-6") == tier_rank("sonnet")

    def test_unknown_full_id_ranks_above_every_known_tier(self) -> None:
        unknown = tier_rank("claude-some-future-model")
        assert unknown > tier_rank("fable")

    def test_none_ranks_as_default_reasoning_tier(self) -> None:
        # None = inherited default; ranked as the conservative reasoning tier so a
        # below-default floor never silently downgrades an inheriting phase.
        assert tier_rank(None) == tier_rank("opus")

    def test_empty_string_ranks_as_default_reasoning_tier(self) -> None:
        assert tier_rank("") == tier_rank("opus")


class TestAttemptCost:
    def test_prefers_reported_cli_cost(self) -> None:
        usage = AttemptUsage(
            model="claude-opus-4-8",
            reported_cost_usd=0.42,
            input_tokens=999_999,
            output_tokens=999_999,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        assert attempt_cost_usd(usage) == pytest.approx(0.42)

    def test_falls_back_to_price_table_when_cost_absent(self) -> None:
        usage = AttemptUsage(
            model="sonnet",
            reported_cost_usd=None,
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        assert attempt_cost_usd(usage) == pytest.approx(3.0)
        assert price_table_cost_usd(usage) == pytest.approx(3.0)


class TestCostBreakdown:
    def test_totals_and_splits_per_tier(self) -> None:
        usages = [
            AttemptUsage("opus", 1.0, 0, 0, 0, 0),
            AttemptUsage("opus", 2.0, 0, 0, 0, 0),
            AttemptUsage("sonnet", 0.5, 0, 0, 0, 0),
        ]
        breakdown = CostBreakdown.from_usages(usages)
        assert breakdown.total_usd == pytest.approx(3.5)
        assert breakdown.attempts == 3
        assert breakdown.per_tier_usd["opus"] == pytest.approx(3.0)
        assert breakdown.per_tier_usd["sonnet"] == pytest.approx(0.5)

    def test_empty_is_zero(self) -> None:
        breakdown = CostBreakdown.from_usages([])
        assert breakdown.total_usd == pytest.approx(0.0)
        assert breakdown.attempts == 0


class TestCycleStart:
    def test_calendar_month_when_no_anchor(self) -> None:
        assert cycle_start(date(2026, 6, 14)) == date(2026, 6, 1)

    def test_anchor_day_most_recent_occurrence(self) -> None:
        # Anchor 15: on the 20th the cycle started this month's 15th.
        assert cycle_start(date(2026, 6, 20), anchor_day=15) == date(2026, 6, 15)

    def test_anchor_day_rolls_to_previous_month_before_anchor(self) -> None:
        # Anchor 15: on the 10th the cycle started LAST month's 15th.
        assert cycle_start(date(2026, 6, 10), anchor_day=15) == date(2026, 5, 15)

    def test_anchor_on_january_rolls_to_december(self) -> None:
        assert cycle_start(date(2026, 1, 5), anchor_day=20) == date(2025, 12, 20)

    def test_anchor_31_clamps_into_short_month(self) -> None:
        # Anchor 31 in February clamps to the 28th (2026 not a leap year).
        assert cycle_start(date(2026, 2, 27), anchor_day=31) == date(2026, 1, 31)
        assert cycle_start(date(2026, 3, 1), anchor_day=31) == date(2026, 2, 28)


class TestProjection:
    def test_linear_projection_scales_to_full_cycle(self) -> None:
        # Spent $50 over the first 10 days of a 30-day (June) calendar cycle.
        projected = project_month_end_usd(50.0, cycle_start_date=date(2026, 6, 1), today=date(2026, 6, 10))
        # 10 days elapsed (incl. today), 30-day cycle → 50 * 30/10 = 150.
        assert projected == pytest.approx(150.0)

    def test_first_day_projects_full_cycle_from_one_day(self) -> None:
        projected = project_month_end_usd(5.0, cycle_start_date=date(2026, 6, 1), today=date(2026, 6, 1))
        assert projected == pytest.approx(150.0)

    def test_december_cycle_projects_into_january(self) -> None:
        # December cycle (31 days): the next cycle start rolls into January.
        projected = project_month_end_usd(31.0, cycle_start_date=date(2026, 12, 1), today=date(2026, 12, 1))
        assert projected == pytest.approx(31.0 * 31)


class TestCostReport:
    def _report(self, total: float) -> CostReport:
        breakdown = CostBreakdown(total_usd=total, per_tier_usd={"opus": total}, attempts=1)
        return CostReport.build(
            breakdown,
            credit_usd=DEFAULT_MONTHLY_CREDIT_USD,
            cycle_start_date=date(2026, 6, 1),
            today=date(2026, 6, 10),
        )

    def test_chip_is_compact_whole_dollars_with_period_label(self) -> None:
        assert self._report(48.4).chip() == "SDK mtd ≈$48/$200"

    def test_chip_stays_tiny_at_high_spend(self) -> None:
        assert self._report(1234.0).chip() == "SDK mtd ≈$1234/$200"

    def test_render_lines_show_credit_and_projection(self) -> None:
        lines = "\n".join(self._report(50.0).render_lines())
        assert "$50.00 / $200 credit (25%)" in lines
        assert "projected end-of-cycle: $150.00" in lines
        assert "opus: $50.00" in lines

    def test_render_lines_omit_per_model_when_no_spend(self) -> None:
        report = CostReport.build(
            CostBreakdown(),
            credit_usd=DEFAULT_MONTHLY_CREDIT_USD,
            cycle_start_date=date(2026, 6, 1),
            today=date(2026, 6, 10),
        )
        lines = "\n".join(report.render_lines())
        assert "per model" not in lines
        assert "(0%)" in lines
