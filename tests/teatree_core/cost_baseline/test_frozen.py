"""The committed pre-Opus-5 baseline: loading it, and diffing a live aggregate against it."""

from pathlib import Path

import pytest
import yaml

from teatree.core.cost_baseline.aggregate import AttemptRecord, UsageStats, aggregate_by_phase
from teatree.core.cost_baseline.frozen import FROZEN_BASELINE_PATH, BaselineError, compare_phases, load_frozen_baseline

_MINIMAL = {
    "cutover": {"model": "claude-opus-5", "pull_request": 3731, "merged_at": "2026-07-25T08:59:42Z"},
    "window": {
        "first_attempt_started_at": "2026-07-13T14:25:08Z",
        "last_attempt_started_at": "2026-07-25T03:23:36Z",
    },
    "coverage": {
        "taskattempt_rows_total": 10,
        "real_dispatch_rows": 2,
        "rows_with_cost_usd": 2,
        "rows_with_output_tokens": 2,
        "post_cutover_rows": 0,
    },
    "per_phase": {
        "coding": {
            "attempts": 2,
            "median_output_tokens": 100.0,
            "p95_output_tokens": 200.0,
            "median_billed_input_tokens": 10.0,
            "p95_billed_input_tokens": 20.0,
            "median_num_turns": 3.0,
            "p95_num_turns": 4.0,
            "median_cost_usd": 1.0,
            "p95_cost_usd": 2.0,
            "total_cost_usd": 3.0,
        }
    },
    "per_phase_model": {
        "coding": {
            "claude-opus-4-8": {
                "attempts": 2,
                "median_output_tokens": 100.0,
                "p95_output_tokens": 200.0,
                "median_billed_input_tokens": 10.0,
                "p95_billed_input_tokens": 20.0,
                "median_num_turns": 3.0,
                "p95_num_turns": 4.0,
                "median_cost_usd": 1.0,
                "p95_cost_usd": 2.0,
                "total_cost_usd": 3.0,
            }
        }
    },
}


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


class TestLoadFrozenBaseline:
    def test_parses_the_per_phase_and_per_phase_model_tables(self, tmp_path: Path) -> None:
        baseline = load_frozen_baseline(_write(tmp_path, _MINIMAL))
        assert baseline.per_phase["coding"].attempts == 2
        assert baseline.per_phase_model["coding", "claude-opus-4-8"].total_cost_usd == pytest.approx(3.0)

    def test_carries_the_cutover_and_window_provenance(self, tmp_path: Path) -> None:
        baseline = load_frozen_baseline(_write(tmp_path, _MINIMAL))
        assert baseline.cutover_model == "claude-opus-5"
        assert baseline.last_attempt_started_at == "2026-07-25T03:23:36Z"

    def test_records_that_the_predicates_agree_on_the_real_dispatch_population(self, tmp_path: Path) -> None:
        baseline = load_frozen_baseline(_write(tmp_path, _MINIMAL))
        assert baseline.predicates_agree

    def test_flags_a_baseline_whose_predicates_disagree(self, tmp_path: Path) -> None:
        payload = {**_MINIMAL, "coverage": {**_MINIMAL["coverage"], "rows_with_cost_usd": 1}}
        assert not load_frozen_baseline(_write(tmp_path, payload)).predicates_agree

    def test_a_missing_file_raises_rather_than_yielding_an_empty_baseline(self, tmp_path: Path) -> None:
        with pytest.raises(BaselineError, match="missing"):
            load_frozen_baseline(tmp_path / "absent.yaml")

    def test_a_non_mapping_document_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BaselineError, match="mapping"):
            load_frozen_baseline(_write(tmp_path, ["not", "a", "mapping"]))

    def test_a_group_missing_a_metric_raises_rather_than_defaulting_to_zero(self, tmp_path: Path) -> None:
        payload = {**_MINIMAL, "per_phase": {"coding": {"attempts": 2}}}
        with pytest.raises(BaselineError, match="median_output_tokens"):
            load_frozen_baseline(_write(tmp_path, payload))


class TestCommittedArtifact:
    """The shipped freeze — the thing a post-cutover comparison actually reads."""

    def test_the_committed_baseline_loads(self) -> None:
        assert load_frozen_baseline(FROZEN_BASELINE_PATH).per_phase

    def test_the_committed_baseline_saw_no_post_cutover_attempt(self) -> None:
        assert load_frozen_baseline(FROZEN_BASELINE_PATH).post_cutover_rows == 0

    def test_the_committed_baselines_three_real_dispatch_predicates_agree(self) -> None:
        assert load_frozen_baseline(FROZEN_BASELINE_PATH).predicates_agree

    def test_every_committed_phase_group_rolls_up_its_phase_model_rows(self) -> None:
        baseline = load_frozen_baseline(FROZEN_BASELINE_PATH)
        for phase, stats in baseline.per_phase.items():
            per_model = [s for (p, _), s in baseline.per_phase_model.items() if p == phase]
            assert stats.attempts == sum(s.attempts for s in per_model)


class TestComparePhases:
    def _live(self, *, output_tokens: int, cost_usd: float) -> dict[str, UsageStats]:
        record = AttemptRecord(
            phase="coding",
            model="claude-opus-5",
            input_tokens=10,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
            num_turns=3,
            cost_usd=cost_usd,
        )
        return aggregate_by_phase([record])

    def test_reports_the_ratio_of_live_to_frozen_median_output_tokens(self, tmp_path: Path) -> None:
        baseline = load_frozen_baseline(_write(tmp_path, _MINIMAL))
        comparison = compare_phases(self._live(output_tokens=1200, cost_usd=1.0), baseline)
        assert [c.phase for c in comparison] == ["coding"]
        assert comparison[0].median_output_tokens_ratio == pytest.approx(12.0)

    def test_reports_the_ratio_of_live_to_frozen_median_cost(self, tmp_path: Path) -> None:
        baseline = load_frozen_baseline(_write(tmp_path, _MINIMAL))
        comparison = compare_phases(self._live(output_tokens=100, cost_usd=3.0), baseline)
        assert comparison[0].median_cost_usd_ratio == pytest.approx(3.0)

    def test_a_phase_absent_from_the_baseline_is_reported_as_unbaselined(self, tmp_path: Path) -> None:
        baseline = load_frozen_baseline(_write(tmp_path, _MINIMAL))
        live = aggregate_by_phase(
            [
                AttemptRecord(
                    phase="brand_new_phase",
                    model="claude-opus-5",
                    input_tokens=1,
                    output_tokens=1,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    num_turns=1,
                    cost_usd=1.0,
                )
            ]
        )
        comparison = compare_phases(live, baseline)
        assert comparison[0].baselined is False
        assert comparison[0].median_output_tokens_ratio is None

    def test_a_baseline_phase_with_no_live_attempts_is_omitted_not_reported_as_zero(self, tmp_path: Path) -> None:
        baseline = load_frozen_baseline(_write(tmp_path, _MINIMAL))
        assert compare_phases({}, baseline) == []
