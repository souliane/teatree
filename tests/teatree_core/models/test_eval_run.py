"""Run-history + baseline ledger for the behavioral eval harness.

Covers the Fat-Model surface the later model-regression mode reads:
per-scenario pass-rate aggregation, latest-baseline lookup, single-baseline
invariant per model, and the baseline-vs-candidate regression diff.
"""

import pytest
from django.test import TestCase

from teatree.core.models import EvalRunRecord, EvalScenarioResult, EvalVerdict, MatcherDetail, TrajectoryToolCall


def _record(model: str = "haiku", *, is_baseline: bool = False) -> EvalRunRecord:
    return EvalRunRecord.objects.record(model=model, suite="core", is_baseline=is_baseline)


class TestRecordScenario(TestCase):
    def test_persists_trajectory_and_side_effect_signals(self) -> None:
        run = _record()
        result = run.record_scenario(
            scenario_name="worktree_first",
            verdict=EvalVerdict.PASS,
            terminal_reason="success",
            tool_calls=[TrajectoryToolCall(name="Bash", input={"command": "git worktree add"}, turn=1)],
            matcher_details=[
                MatcherDetail(
                    kind="positive", tool="Bash", arg_path="command", operator="contains", value="x", passed=True
                )
            ],
        )

        result.refresh_from_db()
        assert result.run_id == run.pk
        assert result.tool_calls[0]["name"] == "Bash"
        assert result.terminal_reason == "success"
        assert result.matcher_details[0]["passed"] is True

    def test_trial_defaults_to_zero_for_single_trial_runs(self) -> None:
        run = _record()
        result = run.record_scenario(scenario_name="doc_update", verdict=EvalVerdict.FAIL)
        assert result.trial == 0

    def test_persists_token_columns_when_given(self) -> None:
        run = _record()
        result = run.record_scenario(
            scenario_name="worktree_first",
            verdict=EvalVerdict.PASS,
            input_tokens=120,
            cache_creation_tokens=340,
            cache_read_tokens=6500,
            output_tokens=80,
        )
        result.refresh_from_db()
        assert result.input_tokens == 120
        assert result.cache_creation_tokens == 340
        assert result.cache_read_tokens == 6500
        assert result.output_tokens == 80

    def test_token_columns_default_to_null_for_legacy_rows(self) -> None:
        # NULL (not 0) distinguishes a legacy/subscription row with no usage
        # signal from a real metered 0.
        run = _record()
        result = run.record_scenario(scenario_name="doc_update", verdict=EvalVerdict.FAIL)
        result.refresh_from_db()
        assert result.input_tokens is None
        assert result.cache_creation_tokens is None
        assert result.cache_read_tokens is None
        assert result.output_tokens is None


class TestRunCounts(TestCase):
    def test_counts_partition_by_verdict(self) -> None:
        run = _record()
        run.record_scenario(scenario_name="a", verdict=EvalVerdict.PASS)
        run.record_scenario(scenario_name="b", verdict=EvalVerdict.FAIL)
        run.record_scenario(scenario_name="c", verdict=EvalVerdict.SKIP)

        assert run.total == 3
        assert run.passed == 1
        assert run.failed == 1
        assert run.skipped == 1


class TestPassRates(TestCase):
    def test_aggregates_per_scenario_excluding_skips(self) -> None:
        run = _record()
        run.record_scenario(scenario_name="a", trial=0, verdict=EvalVerdict.PASS)
        run.record_scenario(scenario_name="a", trial=1, verdict=EvalVerdict.FAIL)
        run.record_scenario(scenario_name="b", trial=0, verdict=EvalVerdict.SKIP)

        rates = {r.scenario_name: r for r in run.pass_rates()}

        assert rates["a"].total == 2
        assert rates["a"].passed == 1
        assert rates["a"].pass_rate == pytest.approx(0.5)
        assert "b" not in rates

    def test_queryset_pass_rates_zero_total_is_zero_rate(self) -> None:
        run = _record()
        run.record_scenario(scenario_name="only_skip", verdict=EvalVerdict.SKIP)
        assert EvalScenarioResult.objects.filter(run=run).pass_rates() == []


class TestBaseline(TestCase):
    def test_latest_baseline_is_most_recent(self) -> None:
        older = _record(is_baseline=True)
        newer = _record(is_baseline=True)
        _record(is_baseline=False)

        latest = EvalRunRecord.objects.latest_baseline()

        assert latest is not None
        assert latest.pk == newer.pk
        assert latest.pk != older.pk

    def test_latest_baseline_none_when_no_baseline(self) -> None:
        _record(is_baseline=False)
        assert EvalRunRecord.objects.latest_baseline() is None

    def test_mark_baseline_demotes_prior_baseline_for_same_model(self) -> None:
        prior = _record(model="haiku", is_baseline=True)
        fresh = _record(model="haiku")

        fresh.mark_baseline()
        prior.refresh_from_db()

        assert fresh.is_baseline is True
        assert prior.is_baseline is False

    def test_mark_baseline_leaves_other_models_baseline_intact(self) -> None:
        other_model = _record(model="sonnet", is_baseline=True)
        fresh = _record(model="haiku")

        fresh.mark_baseline()
        other_model.refresh_from_db()

        assert other_model.is_baseline is True


class TestRegressionDiff(TestCase):
    def test_flags_scenario_whose_pass_rate_dropped(self) -> None:
        baseline = _record(model="haiku", is_baseline=True)
        baseline.record_scenario(scenario_name="stable", verdict=EvalVerdict.PASS)
        baseline.record_scenario(scenario_name="regressed", verdict=EvalVerdict.PASS)

        candidate = _record(model="opus")
        candidate.record_scenario(scenario_name="stable", verdict=EvalVerdict.PASS)
        candidate.record_scenario(scenario_name="regressed", verdict=EvalVerdict.FAIL)

        diff = {d.scenario_name: d for d in EvalRunRecord.regression_diff(baseline=baseline, candidate=candidate)}

        assert diff["stable"].regressed is False
        assert diff["stable"].delta == pytest.approx(0.0)
        assert diff["regressed"].regressed is True
        assert diff["regressed"].delta == pytest.approx(-1.0)

    def test_scenario_only_in_one_run_defaults_missing_side_to_zero(self) -> None:
        baseline = _record(model="haiku", is_baseline=True)
        baseline.record_scenario(scenario_name="dropped", verdict=EvalVerdict.PASS)

        candidate = _record(model="opus")
        candidate.record_scenario(scenario_name="added", verdict=EvalVerdict.PASS)

        diff = {d.scenario_name: d for d in EvalRunRecord.regression_diff(baseline=baseline, candidate=candidate)}

        assert diff["dropped"].candidate_pass_rate == pytest.approx(0.0)
        assert diff["dropped"].regressed is True
        assert diff["added"].baseline_pass_rate == pytest.approx(0.0)
        assert diff["added"].regressed is False


class TestScenarioCost(TestCase):
    def test_cost_usd_round_trips_and_defaults_to_zero(self) -> None:
        run = _record()
        metered = run.record_scenario(scenario_name="metered", verdict=EvalVerdict.PASS, cost_usd=0.42)
        free = run.record_scenario(scenario_name="free", verdict=EvalVerdict.PASS)

        metered.refresh_from_db()
        free.refresh_from_db()
        assert metered.cost_usd == pytest.approx(0.42)
        assert free.cost_usd == pytest.approx(0.0)


class TestCostRegressionDiff(TestCase):
    def test_flags_scenario_whose_cost_rose(self) -> None:
        baseline = _record(model="haiku", is_baseline=True)
        baseline.record_scenario(scenario_name="cheap", verdict=EvalVerdict.PASS, cost_usd=0.10)
        baseline.record_scenario(scenario_name="spiking", verdict=EvalVerdict.PASS, cost_usd=0.10)

        candidate = _record(model="opus")
        candidate.record_scenario(scenario_name="cheap", verdict=EvalVerdict.PASS, cost_usd=0.10)
        candidate.record_scenario(scenario_name="spiking", verdict=EvalVerdict.PASS, cost_usd=0.30)

        diff = {d.scenario_name: d for d in EvalRunRecord.cost_regression_diff(baseline=baseline, candidate=candidate)}

        assert diff["cheap"].delta == pytest.approx(0.0)
        assert diff["cheap"].pct_increase == pytest.approx(0.0)
        assert diff["spiking"].delta == pytest.approx(0.20)
        assert diff["spiking"].pct_increase == pytest.approx(2.0)

    def test_cost_decrease_is_negative_pct_not_a_regression(self) -> None:
        baseline = _record(model="haiku", is_baseline=True)
        baseline.record_scenario(scenario_name="cheaper", verdict=EvalVerdict.PASS, cost_usd=0.40)

        candidate = _record(model="opus")
        candidate.record_scenario(scenario_name="cheaper", verdict=EvalVerdict.PASS, cost_usd=0.20)

        diff = {d.scenario_name: d for d in EvalRunRecord.cost_regression_diff(baseline=baseline, candidate=candidate)}

        assert diff["cheaper"].delta == pytest.approx(-0.20)
        assert diff["cheaper"].pct_increase == pytest.approx(-0.5)

    def test_zero_baseline_cost_yields_undefined_pct_no_div_by_zero(self) -> None:
        baseline = _record(model="haiku", is_baseline=True)
        baseline.record_scenario(scenario_name="free_baseline", verdict=EvalVerdict.PASS, cost_usd=0.0)

        candidate = _record(model="opus")
        candidate.record_scenario(scenario_name="free_baseline", verdict=EvalVerdict.PASS, cost_usd=0.25)

        diff = {d.scenario_name: d for d in EvalRunRecord.cost_regression_diff(baseline=baseline, candidate=candidate)}

        assert diff["free_baseline"].baseline_cost_usd == pytest.approx(0.0)
        assert diff["free_baseline"].delta == pytest.approx(0.25)
        assert diff["free_baseline"].pct_increase is None


class TestStr(TestCase):
    def test_run_str_tags_baseline(self) -> None:
        assert "baseline" in str(_record(is_baseline=True))
        assert "run<" in str(_record(is_baseline=False))

    def test_result_str_names_scenario_and_verdict(self) -> None:
        run = _record()
        result = run.record_scenario(scenario_name="worktree_first", verdict=EvalVerdict.PASS)
        rendered = str(result)
        assert "worktree_first" in rendered
        assert "pass" in rendered
