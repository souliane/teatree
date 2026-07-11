"""Cache-hit telemetry + estimate flagging in the cost report (#3157 E2d / E5).

Acceptance: a router-lane attempt records the router-REPORTED cost (flagged not-estimated);
a price-table figure is flagged estimated; ``t3 cost`` shows cache-hit % per lane/phase.
"""

import dataclasses
from datetime import date

import pytest

from teatree.agents.headless_usage import _attempt_usage
from teatree.core.cost import AttemptUsage, CostBreakdown, CostReport
from tests.teatree_agents._sdk_fake import result_message

_BASE_USAGE = AttemptUsage(
    model="claude-opus-4-8",
    reported_cost_usd=1.0,
    input_tokens=100,
    output_tokens=50,
    cache_read_tokens=0,
    cache_write_tokens=0,
)


def _usage(**over: object) -> AttemptUsage:
    """A base :class:`AttemptUsage` with the named fields overridden."""
    return dataclasses.replace(_BASE_USAGE, **over)


class TestCacheHitRatio:
    def test_per_lane_cache_hit_ratio_is_reads_over_cacheable_input(self) -> None:
        # metered: 900 cache reads of 1000 cacheable input (900 read + 0 write + 100 input) → 90%.
        breakdown = CostBreakdown.from_usages(
            [_usage(lane="metered", cache_read_tokens=900, cache_write_tokens=0, input_tokens=100)]
        )
        assert breakdown.per_lane_cache_hit_ratio["metered"] == pytest.approx(0.9)

    def test_per_lane_ratio_aggregates_across_attempts(self) -> None:
        breakdown = CostBreakdown.from_usages(
            [
                _usage(lane="metered", cache_read_tokens=100, input_tokens=0, cache_write_tokens=0),
                _usage(lane="metered", cache_read_tokens=0, input_tokens=100, cache_write_tokens=0),
            ]
        )
        # 100 reads / 200 cacheable → 50%, aggregated not per-attempt-averaged.
        assert breakdown.per_lane_cache_hit_ratio["metered"] == pytest.approx(0.5)

    def test_a_lane_with_no_cacheable_input_is_omitted_not_zero(self) -> None:
        breakdown = CostBreakdown.from_usages(
            [_usage(lane="metered", cache_read_tokens=0, cache_write_tokens=0, input_tokens=0)]
        )
        assert "metered" not in breakdown.per_lane_cache_hit_ratio

    def test_per_phase_cache_hit_ratio(self) -> None:
        breakdown = CostBreakdown.from_usages(
            [_usage(phase="coding", cache_read_tokens=750, input_tokens=250, cache_write_tokens=0)]
        )
        assert breakdown.per_phase_cache_hit_ratio["coding"] == pytest.approx(0.75)


class TestEstimateFlagging:
    def test_estimated_usd_totals_only_flagged_attempts(self) -> None:
        breakdown = CostBreakdown.from_usages(
            [
                _usage(reported_cost_usd=2.0, estimated=True),
                _usage(reported_cost_usd=3.0, estimated=False),
            ]
        )
        assert breakdown.total_usd == pytest.approx(5.0)
        assert breakdown.estimated_usd == pytest.approx(2.0)

    def test_report_flags_estimated_spend_and_cache_hits(self) -> None:
        breakdown = CostBreakdown.from_usages(
            [_usage(lane="metered", reported_cost_usd=4.0, estimated=True, cache_read_tokens=90, input_tokens=10)]
        )
        report = CostReport.build(
            breakdown, credit_usd=200.0, cycle_start_date=date(2026, 7, 1), today=date(2026, 7, 11)
        )
        lines = "\n".join(report.render_lines())
        assert "estimated (price-table" in lines
        assert "cache-hit 90%" in lines


class TestReportedCostPassthroughFlagging:
    def test_reported_total_cost_is_not_estimated(self) -> None:
        # A ResultMessage carrying total_cost_usd (CLI/SDK OR router-reported, #3157 E5) is
        # recorded as the reported figure and NOT flagged estimated.
        message = result_message(
            total_cost_usd=0.42,
            usage={"input_tokens": 100, "output_tokens": 10},
            model_usage={"claude-opus-4-8": {}},
        )
        usage = _attempt_usage(message, lane="metered")
        assert usage.cost_usd == pytest.approx(0.42)
        assert usage.cost_is_estimated is False

    def test_price_table_fallback_is_flagged_estimated(self) -> None:
        # No reported cost → the price-table estimate, flagged estimated.
        message = result_message(
            total_cost_usd=None,
            usage={"input_tokens": 1000, "output_tokens": 100},
            model_usage={"claude-opus-4-8": {}},
        )
        usage = _attempt_usage(message, lane="metered")
        assert usage.cost_usd is not None
        assert usage.cost_is_estimated is True
