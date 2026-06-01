"""Run-store bridge and baseline-diff tests (#1160).

``record_run`` persists model-agnostic :class:`ScenarioOutcome` value objects
under one ``run_id``; ``diff_against_baseline`` compares a recorded run against
each model's preceding recorded run and classifies regressions / improvements /
new scenarios.
"""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import EvalRunRecord
from teatree.eval.run_store import ScenarioOutcome, diff_against_baseline, record_run
from tests.factories import EvalRunRecordFactory


def _outcome(scenario: str, *, model: str = "haiku", passed: bool = True, score: float = 1.0) -> ScenarioOutcome:
    return ScenarioOutcome(scenario=scenario, model=model, passed=passed, score=score, trials=1)


class TestRecordRun(TestCase):
    def test_persists_outcomes_under_one_run_id(self) -> None:
        run_id = record_run([_outcome("a"), _outcome("b")], git_sha="deadbeef")

        rows = EvalRunRecord.objects.filter(run_id=run_id)
        assert rows.count() == 2
        assert {r.scenario for r in rows} == {"a", "b"}
        assert all(r.git_sha == "deadbeef" for r in rows)

    def test_explicit_run_id_is_honored(self) -> None:
        record_run([_outcome("a")], run_id="fixed", git_sha="")
        assert EvalRunRecord.objects.filter(run_id="fixed").exists()

    def test_skipped_outcome_recorded_as_skipped(self) -> None:
        run_id = record_run(
            [ScenarioOutcome(scenario="s", model="haiku", passed=False, score=0.0, trials=1, skipped=True)],
            git_sha="",
        )
        row = EvalRunRecord.objects.get(run_id=run_id, scenario="s")
        assert row.skipped is True


class TestDiffAgainstBaseline(TestCase):
    def test_flags_regression(self) -> None:
        base = timezone.now()
        EvalRunRecordFactory(
            run_id="prior", model="haiku", scenario="s", score=1.0, recorded_at=base - dt.timedelta(hours=1)
        )
        EvalRunRecordFactory(run_id="curr", model="haiku", scenario="s", failing=True, recorded_at=base)

        report = diff_against_baseline("curr")

        assert not report.ok
        assert len(report.regressions) == 1
        regression = report.regressions[0]
        assert regression.scenario == "s"
        assert regression.baseline_score == pytest.approx(1.0)
        assert regression.current_score == pytest.approx(0.0)

    def test_flags_improvement_and_stays_ok(self) -> None:
        base = timezone.now()
        EvalRunRecordFactory(
            run_id="prior", model="haiku", scenario="s", failing=True, recorded_at=base - dt.timedelta(hours=1)
        )
        EvalRunRecordFactory(run_id="curr", model="haiku", scenario="s", score=1.0, recorded_at=base)

        report = diff_against_baseline("curr")

        assert report.ok
        assert len(report.improvements) == 1

    def test_new_scenario_without_baseline_is_not_a_regression(self) -> None:
        EvalRunRecordFactory(run_id="curr", model="haiku", scenario="brand_new", failing=True)

        report = diff_against_baseline("curr")

        assert report.ok
        assert report.new_scenarios == ("brand_new",)
        assert report.regressions == ()

    def test_per_model_baselines_are_independent(self) -> None:
        base = timezone.now()
        EvalRunRecordFactory(
            run_id="prior_h", model="haiku", scenario="s", score=1.0, recorded_at=base - dt.timedelta(hours=2)
        )
        EvalRunRecordFactory(
            run_id="prior_o", model="opus", scenario="s", failing=True, recorded_at=base - dt.timedelta(hours=2)
        )
        EvalRunRecordFactory(run_id="curr", model="haiku", scenario="s", failing=True, recorded_at=base)
        EvalRunRecordFactory(run_id="curr", model="opus", scenario="s", score=1.0, recorded_at=base)

        report = diff_against_baseline("curr")

        regressed = {(e.scenario, e.model) for e in report.regressions}
        improved = {(e.scenario, e.model) for e in report.improvements}
        assert regressed == {("s", "haiku")}
        assert improved == {("s", "opus")}

    def test_skipped_rows_ignored(self) -> None:
        base = timezone.now()
        EvalRunRecordFactory(
            run_id="prior", model="haiku", scenario="s", score=1.0, recorded_at=base - dt.timedelta(hours=1)
        )
        EvalRunRecordFactory(run_id="curr", model="haiku", scenario="s", was_skipped=True, recorded_at=base)

        report = diff_against_baseline("curr")

        assert report.entries == ()
        assert report.ok
