"""Per-phase / per-(phase, model) usage aggregation over real dispatch attempts."""

import dataclasses

import pytest

from teatree.core.cost_baseline.aggregate import (
    AttemptRecord,
    UsageStats,
    aggregate_by_phase,
    aggregate_by_phase_model,
    percentile,
)

_DEFAULT_RECORD = AttemptRecord(
    phase="coding",
    model="claude-opus-4-8",
    input_tokens=10,
    output_tokens=100,
    cache_read_tokens=5,
    cache_write_tokens=2,
    num_turns=3,
    cost_usd=1.0,
)


def _record(**overrides: float | str) -> AttemptRecord:
    return dataclasses.replace(_DEFAULT_RECORD, **overrides)


class TestPercentile:
    """Nearest-rank, so a small group never interpolates a value nothing measured."""

    def test_median_of_odd_sample_is_the_middle_observation(self) -> None:
        assert percentile([3.0, 1.0, 2.0], 0.5) == pytest.approx(2.0)

    def test_median_of_even_sample_takes_the_lower_of_the_two_middles(self) -> None:
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.0)

    def test_p95_of_a_single_observation_is_that_observation(self) -> None:
        assert percentile([7.0], 0.95) == pytest.approx(7.0)

    def test_p95_reaches_the_tail_where_the_median_does_not(self) -> None:
        values = [float(n) for n in range(1, 11)]
        assert percentile(values, 0.5) == pytest.approx(5.0)
        assert percentile(values, 0.95) == pytest.approx(10.0)

    def test_p95_still_excludes_an_outlier_thinner_than_the_top_five_percent(self) -> None:
        assert percentile([1.0] * 19 + [1000.0], 0.95) == pytest.approx(1.0)

    def test_empty_sample_raises_rather_than_returning_zero(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            percentile([], 0.5)


class TestAggregateByPhaseModel:
    def test_groups_on_the_phase_and_model_pair(self) -> None:
        records = [
            _record(phase="coding", model="a"),
            _record(phase="coding", model="b"),
            _record(phase="reviewing", model="a"),
        ]
        stats = aggregate_by_phase_model(records)
        assert set(stats) == {("coding", "a"), ("coding", "b"), ("reviewing", "a")}

    def test_billed_input_sums_prompt_plus_both_cache_dimensions(self) -> None:
        stats = aggregate_by_phase_model([_record(input_tokens=10, cache_read_tokens=5, cache_write_tokens=2)])
        assert stats["coding", "claude-opus-4-8"].median_billed_input_tokens == pytest.approx(17.0)

    def test_total_cost_sums_every_attempt_in_the_group(self) -> None:
        records = [_record(cost_usd=1.5), _record(cost_usd=2.5)]
        assert aggregate_by_phase_model(records)["coding", "claude-opus-4-8"].total_cost_usd == pytest.approx(4.0)

    def test_p95_separates_from_median_so_variance_is_visible(self) -> None:
        records = [_record(output_tokens=100) for _ in range(9)] + [_record(output_tokens=9_000)]
        group = aggregate_by_phase_model(records)["coding", "claude-opus-4-8"]
        assert group.median_output_tokens == pytest.approx(100.0)
        assert group.p95_output_tokens == pytest.approx(9_000.0)

    def test_attempts_counts_the_group(self) -> None:
        assert aggregate_by_phase_model([_record(), _record()])["coding", "claude-opus-4-8"].attempts == 2

    def test_no_records_yields_no_groups(self) -> None:
        assert aggregate_by_phase_model([]) == {}


class TestAggregateByPhase:
    """The cutover comparison is per PHASE — the model changes by construction."""

    def test_rolls_every_model_of_a_phase_into_one_group(self) -> None:
        records = [
            _record(phase="coding", model="old", cost_usd=1.0),
            _record(phase="coding", model="new", cost_usd=3.0),
        ]
        stats = aggregate_by_phase(records)
        assert set(stats) == {"coding"}
        assert stats["coding"].attempts == 2
        assert stats["coding"].total_cost_usd == pytest.approx(4.0)


class TestUsageStats:
    def test_is_hashable_and_frozen_so_a_loaded_baseline_cannot_drift(self) -> None:
        stats = aggregate_by_phase([_record()])["coding"]
        assert isinstance(stats, UsageStats)
        with pytest.raises(AttributeError):
            stats.attempts = 99  # ty: ignore[invalid-assignment]
