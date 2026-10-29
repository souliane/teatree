"""The published results surface is a live rendering of the recorded ledger."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import EvalRunRecord, EvalVerdict
from teatree.eval.results_page import render_results_page


class TestResultsPage(TestCase):
    def test_measured_nothing_is_not_presented_as_a_pass(self) -> None:
        now = timezone.now()
        run = EvalRunRecord.objects.create(model="subscription", suite="eval_local:1", started_at=now)
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)

        page = render_results_page(now=now)

        assert now.date().isoformat() in page.splitlines()[0]
        assert "MEASURED NOTHING" in page
        assert "0 passed" not in page

    def test_stale_measured_run_lists_failing_scenarios(self) -> None:
        now = timezone.now()
        run = EvalRunRecord.objects.create(
            model="subscription", suite="eval_local:1", started_at=now - timedelta(days=8)
        )
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.FAIL, cost_usd=0.25)

        page = render_results_page(now=now)

        assert "STALE:" in page
        assert "alpha" in page
        assert "0 passed" in page
        assert "1 failed" in page

    def test_error_only_run_names_the_failed_scenario(self) -> None:
        run = EvalRunRecord.objects.create(model="subscription", suite="eval_local:1")
        run.record_scenario(scenario_name="broken-lane", verdict=EvalVerdict.ERROR)

        page = render_results_page()

        assert "MEASURED NOTHING" in page
        assert "broken-lane" in page

    def test_newer_manual_run_does_not_replace_the_local_result(self) -> None:
        local = EvalRunRecord.objects.create(model="subscription", suite="eval_local:1")
        local.record_scenario(scenario_name="local-failure", verdict=EvalVerdict.FAIL)
        manual = EvalRunRecord.objects.create(model="manual", suite="other")
        manual.record_scenario(scenario_name="manual-pass", verdict=EvalVerdict.PASS)

        page = render_results_page()

        assert "local-failure" in page
        assert "manual-pass" not in page
