"""Durable eval run-store ledger tests (#1160 baseline/history).

The record dedupes a re-recorded ``(run_id, scenario, model)`` triple, groups
rows into runs for the history listing, and resolves the most-recent prior run
per model as the regression baseline.
"""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import EvalRunRecord
from tests.factories import EvalRunRecordFactory


class TestRecordScenario(TestCase):
    def test_inserts_first_observation(self) -> None:
        row = EvalRunRecord.objects.record_scenario(
            run_id="r1",
            scenario="worktree_first",
            model="haiku",
            passed=True,
            score=1.0,
            trials=1,
            git_sha="abc",
        )

        assert row.run_id == "r1"
        assert row.scenario == "worktree_first"
        assert row.passed is True
        assert row.score == pytest.approx(1.0)

    def test_re_record_same_triple_updates_in_place(self) -> None:
        EvalRunRecord.objects.record_scenario(
            run_id="r1", scenario="s", model="haiku", passed=False, score=0.0, trials=1
        )
        EvalRunRecord.objects.record_scenario(
            run_id="r1", scenario="s", model="haiku", passed=True, score=1.0, trials=1
        )

        rows = EvalRunRecord.objects.filter(run_id="r1", scenario="s", model="haiku")
        assert rows.count() == 1
        assert rows.first().passed is True

    def test_distinct_model_is_a_separate_row(self) -> None:
        EvalRunRecord.objects.record_scenario(
            run_id="r1", scenario="s", model="haiku", passed=True, score=1.0, trials=1
        )
        EvalRunRecord.objects.record_scenario(run_id="r1", scenario="s", model="opus", passed=True, score=1.0, trials=1)

        assert EvalRunRecord.objects.filter(run_id="r1", scenario="s").count() == 2


class TestRuns(TestCase):
    def test_groups_rows_into_runs_newest_first(self) -> None:
        older = timezone.now() - dt.timedelta(hours=2)
        newer = timezone.now()
        EvalRunRecordFactory(run_id="old", recorded_at=older)
        EvalRunRecordFactory(run_id="old", recorded_at=older)
        EvalRunRecordFactory(run_id="new", recorded_at=newer)

        runs = EvalRunRecord.objects.runs()

        assert [r.run_id for r in runs] == ["new", "old"]
        assert runs[1].total == 2

    def test_tallies_pass_fail_skip(self) -> None:
        EvalRunRecordFactory(run_id="r")
        EvalRunRecordFactory(run_id="r", failing=True)
        EvalRunRecordFactory(run_id="r", was_skipped=True)

        run = EvalRunRecord.objects.runs()[0]

        assert run.passed == 1
        assert run.failed == 1
        assert run.skipped == 1
        assert run.total == 3

    def test_model_filter(self) -> None:
        EvalRunRecordFactory(run_id="r_haiku", model="haiku")
        EvalRunRecordFactory(run_id="r_opus", model="opus")

        runs = EvalRunRecord.objects.runs(model="opus")

        assert [r.run_id for r in runs] == ["r_opus"]

    def test_limit_caps_distinct_runs(self) -> None:
        base = timezone.now()
        for i in range(5):
            EvalRunRecordFactory(run_id=f"run{i}", recorded_at=base - dt.timedelta(minutes=i))

        runs = EvalRunRecord.objects.runs(limit=2)

        assert len(runs) == 2


class TestBaselineForModel(TestCase):
    def test_returns_most_recent_prior_run(self) -> None:
        base = timezone.now()
        EvalRunRecordFactory(run_id="r1", model="haiku", scenario="s", recorded_at=base - dt.timedelta(hours=2))
        EvalRunRecordFactory(run_id="r2", model="haiku", scenario="s", recorded_at=base - dt.timedelta(hours=1))
        EvalRunRecordFactory(run_id="r3", model="haiku", scenario="s", recorded_at=base)

        baseline = EvalRunRecord.objects.baseline_for_model("haiku", before_run_id="r3")

        assert [b.run_id for b in baseline] == ["r2"]

    def test_empty_when_no_prior_run(self) -> None:
        EvalRunRecordFactory(run_id="only", model="haiku")

        baseline = EvalRunRecord.objects.baseline_for_model("haiku", before_run_id="only")

        assert list(baseline) == []

    def test_latest_run_when_no_before_filter(self) -> None:
        base = timezone.now()
        EvalRunRecordFactory(run_id="r1", model="opus", recorded_at=base - dt.timedelta(hours=1))
        EvalRunRecordFactory(run_id="r2", model="opus", recorded_at=base)

        baseline = EvalRunRecord.objects.baseline_for_model("opus")

        assert {b.run_id for b in baseline} == {"r2"}


class TestStr(TestCase):
    def test_renders_scenario_model_and_verdict(self) -> None:
        row = EvalRunRecordFactory(scenario="worktree_first", model="haiku")
        assert "worktree_first" in str(row)
        assert "haiku" in str(row)
        assert "pass" in str(row)
