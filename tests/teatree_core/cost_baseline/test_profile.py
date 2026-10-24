"""The cost profile: read share, resume-context cost and the addressable upper bounds."""

import dataclasses

import pytest

from teatree.core.cost import ModelPrice
from teatree.core.cost_baseline.profile import (
    Addressable,
    ContextDistribution,
    CostProfile,
    CostProfileError,
    ProfiledAttempt,
    render_lines,
)

_PRICE = ModelPrice(input_per_mtok=10.0, output_per_mtok=50.0)
_CACHE_PREMIUM_PER_MTOK = 12.5 - 1.0

_DEFAULT = ProfiledAttempt(
    attempt_id=1,
    phase="coding",
    tier="opus",
    lane="subscription",
    reasoning_effort="",
    failed=False,
    iteration=1,
    input_tokens=10,
    output_tokens=100,
    cache_read_tokens=40,
    cache_write_tokens=20,
    cost_usd=2.0,
    num_turns=3,
    context_tokens=100_000,
    context_window_tokens=1_000_000,
    carried_context_tokens=None,
    long_tool_calls=None,
    price=_PRICE,
)


def _attempt(**overrides: object) -> ProfiledAttempt:
    return dataclasses.replace(_DEFAULT, **overrides)


def _item(profile: CostProfile, name: str) -> Addressable:
    return next(entry for entry in profile.addressable if entry.item == name)


class TestReadShare:
    def test_overall_share_is_cache_read_over_input_plus_read_plus_write(self) -> None:
        rows = [
            _attempt(attempt_id=1, input_tokens=10, cache_read_tokens=40, cache_write_tokens=20),
            _attempt(attempt_id=2, input_tokens=30, cache_read_tokens=60, cache_write_tokens=0),
        ]

        expected = (40 + 60) / ((10 + 30) + (40 + 60) + (20 + 0))

        assert CostProfile.build(rows).read_share.overall == pytest.approx(expected)

    def test_share_is_weighted_by_tokens_not_averaged_per_attempt(self) -> None:
        small = _attempt(attempt_id=1, input_tokens=1, cache_read_tokens=0, cache_write_tokens=0)
        large = _attempt(attempt_id=2, input_tokens=0, cache_read_tokens=900, cache_write_tokens=99)

        share = CostProfile.build([small, large]).read_share.overall

        assert share == pytest.approx(900 / 1000)

    def test_shares_split_per_lane_phase_and_tier(self) -> None:
        rows = [
            _attempt(attempt_id=1, lane="metered", phase="coding", tier="opus", cache_read_tokens=90),
            _attempt(attempt_id=2, lane="subscription", phase="reviewing", tier="sonnet", cache_read_tokens=10),
        ]

        shares = CostProfile.build(rows).read_share

        assert shares.per_lane["metered"] == pytest.approx(90 / (10 + 90 + 20))
        assert shares.per_lane["subscription"] == pytest.approx(10 / (10 + 10 + 20))
        assert set(shares.per_phase) == {"coding", "reviewing"}
        assert set(shares.per_tier) == {"opus", "sonnet"}

    def test_a_group_with_no_input_tokens_is_unmeasured_never_zero(self) -> None:
        empty = _attempt(attempt_id=1, lane="managed", input_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
        rows = [empty, _attempt(attempt_id=2, lane="metered")]

        shares = CostProfile.build(rows).read_share

        assert shares.per_lane["managed"] is None
        assert shares.per_lane["metered"] is not None


class TestContextDistribution:
    def _distribution(self, contexts: list[int | None]) -> ContextDistribution:
        rows = [_attempt(attempt_id=n, context_tokens=value) for n, value in enumerate(contexts, start=1)]
        return CostProfile.build(rows).context

    def test_percentiles_are_nearest_rank_observations(self) -> None:
        distribution = self._distribution([10_000 * n for n in range(1, 11)])

        assert distribution.p50_tokens == 50_000
        assert distribution.p95_tokens == 100_000

    def test_null_context_attempts_are_counted_apart_not_as_zero(self) -> None:
        distribution = self._distribution([100_000, None, 300_000])

        assert distribution.measured_attempts == 2
        assert distribution.unmeasured_attempts == 1
        assert distribution.p50_tokens == 100_000

    def test_attempt_and_cost_share_above_each_threshold(self) -> None:
        rows = [
            _attempt(attempt_id=1, context_tokens=50_000, cost_usd=1.0),
            _attempt(attempt_id=2, context_tokens=150_000, cost_usd=1.0),
            _attempt(attempt_id=3, context_tokens=250_000, cost_usd=2.0),
            _attempt(attempt_id=4, context_tokens=350_000, cost_usd=4.0),
        ]

        above = {entry.threshold_tokens: entry for entry in CostProfile.build(rows).context.above}

        assert above[120_000].attempt_share == pytest.approx(3 / 4)
        assert above[120_000].cost_share == pytest.approx(7 / 8)
        assert above[200_000].attempt_share == pytest.approx(2 / 4)
        assert above[300_000].cost_share == pytest.approx(4 / 8)


class TestEffortBaseline:
    def test_cells_separate_reasoning_effort_within_a_phase_and_tier(self) -> None:
        rows = [
            _attempt(attempt_id=1, reasoning_effort="high", failed=True, iteration=2),
            _attempt(attempt_id=2, reasoning_effort="high", failed=False, iteration=4),
            _attempt(attempt_id=3, reasoning_effort="low", failed=False, iteration=1),
        ]

        cells = {cell.reasoning_effort: cell for cell in CostProfile.build(rows).effort_cells}

        assert cells["high"].stats.attempts == 2
        assert cells["high"].failure_rate == pytest.approx(0.5)
        assert cells["high"].mean_iteration == pytest.approx(3.0)
        assert cells["low"].failure_rate == pytest.approx(0.0)


class TestAddressableBounds:
    def test_resume_context_counts_the_cost_of_reattempts_carrying_over_the_cap(self) -> None:
        rows = [
            _attempt(attempt_id=1, carried_context_tokens=250_000, cost_usd=3.0),
            _attempt(attempt_id=2, carried_context_tokens=150_000, cost_usd=5.0),
            _attempt(attempt_id=3, carried_context_tokens=None, cost_usd=7.0),
        ]

        item = _item(CostProfile.build(rows), "item2_resume_context")

        assert item.usd_upper_bound == pytest.approx(3.0)
        assert item.share_of_total == pytest.approx(3.0 / 15.0)
        assert item.measured_attempts == 2
        assert item.unmeasured_attempts == 1

    def test_effort_output_counts_output_token_cost_of_balanced_tier_only(self) -> None:
        rows = [
            _attempt(attempt_id=1, tier="sonnet", output_tokens=2_000_000),
            _attempt(attempt_id=2, tier="opus", output_tokens=9_000_000),
        ]

        item = _item(CostProfile.build(rows), "item3_effort_output")

        assert item.usd_upper_bound == pytest.approx(2_000_000 * 50.0 / 1_000_000)
        assert item.flagged_attempts == 1

    def test_long_waits_price_each_wait_at_the_cache_write_premium_over_the_context(self) -> None:
        rows = [
            _attempt(attempt_id=1, context_tokens=200_000, long_tool_calls=3),
            _attempt(attempt_id=2, context_tokens=200_000, long_tool_calls=0),
            _attempt(attempt_id=3, context_tokens=200_000, long_tool_calls=None),
        ]

        item = _item(CostProfile.build(rows), "item4_long_waits")

        assert item.usd_upper_bound == pytest.approx(3 * 200_000 * _CACHE_PREMIUM_PER_MTOK / 1_000_000)
        assert item.measured_attempts == 2
        assert item.unmeasured_attempts == 1

    def test_long_waits_are_unmeasured_not_zero_when_no_trajectory_was_recorded(self) -> None:
        rows = [_attempt(attempt_id=n, long_tool_calls=None) for n in (1, 2)]

        item = _item(CostProfile.build(rows), "item4_long_waits")

        assert item.usd_upper_bound is None
        assert item.share_of_total is None
        assert item.unmeasured_attempts == 2


class TestUnionAndOverlap:
    def _overlapping(self) -> list[ProfiledAttempt]:
        return [
            _attempt(
                attempt_id=1,
                tier="sonnet",
                output_tokens=1_000_000,
                carried_context_tokens=300_000,
                cost_usd=80.0,
            ),
            _attempt(attempt_id=2, tier="opus", carried_context_tokens=250_000, cost_usd=10.0),
        ]

    def test_union_is_below_the_sum_when_two_items_flag_the_same_attempt(self) -> None:
        profile = CostProfile.build(self._overlapping())

        bounds = [entry.usd_upper_bound for entry in profile.addressable if entry.usd_upper_bound is not None]

        assert profile.union.usd < sum(bounds)
        assert profile.union.usd >= max(bounds)
        assert profile.union.usd == pytest.approx(80.0 + 10.0)
        assert profile.union.sum_of_bounds_usd == pytest.approx(sum(bounds))

    def test_overlap_matrix_counts_attempts_flagged_by_both_items(self) -> None:
        overlaps = {(cell.first, cell.second): cell for cell in CostProfile.build(self._overlapping()).union.overlaps}

        cell = overlaps["item2_resume_context", "item3_effort_output"]

        assert cell.attempts == 1
        assert cell.usd == pytest.approx(50.0)

    def test_union_equals_the_sum_when_no_attempt_is_flagged_twice(self) -> None:
        rows = [
            _attempt(attempt_id=1, carried_context_tokens=300_000, cost_usd=4.0),
            _attempt(attempt_id=2, tier="sonnet", output_tokens=1_000_000, cost_usd=1.0),
        ]

        profile = CostProfile.build(rows)

        assert profile.union.usd == pytest.approx(profile.union.sum_of_bounds_usd)
        assert profile.union.flagged_attempts == 2


class TestFailLoud:
    def test_an_empty_sample_names_its_cause(self) -> None:
        with pytest.raises(CostProfileError, match="no real dispatches"):
            CostProfile.build([])

    def test_all_null_context_names_its_cause(self) -> None:
        rows = [_attempt(attempt_id=n, context_tokens=None) for n in (1, 2)]

        with pytest.raises(CostProfileError, match="context_tokens"):
            CostProfile.build(rows)

    def test_zero_total_cost_leaves_shares_unmeasured(self) -> None:
        rows = [_attempt(attempt_id=1, cost_usd=0.0, carried_context_tokens=300_000)]

        item = _item(CostProfile.build(rows), "item2_resume_context")

        assert item.usd_upper_bound == pytest.approx(0.0)
        assert item.share_of_total is None


class TestRender:
    def test_an_unmeasured_item_claims_no_bound_and_prints_no_percentage(self) -> None:
        rows = [_attempt(attempt_id=n, long_tool_calls=None) for n in (1, 2)]

        line = next(line for line in render_lines(CostProfile.build(rows)) if "item4_long_waits" in line)

        assert "unmeasured" in line
        assert "upper bound" not in line
        assert "%" not in line

    def test_a_measured_but_tiny_share_is_not_rounded_to_zero(self) -> None:
        rows = [_attempt(attempt_id=1, carried_context_tokens=300_000, cost_usd=0.01)]
        rows.append(_attempt(attempt_id=2, cost_usd=100.0, carried_context_tokens=0))

        line = next(line for line in render_lines(CostProfile.build(rows)) if "item2_resume_context" in line)

        assert "<0.1%" in line
        assert "0.0%" not in line
