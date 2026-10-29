"""The published results surface is a live rendering of the recorded ledger."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import EvalRunRecord, EvalVerdict
from teatree.eval.results_page import render_results_page


class TestResultsPage(TestCase):
    def test_measured_nothing_is_not_presented_as_a_pass(self) -> None:
        now = timezone.now()
        run = EvalRunRecord.objects.create(model="subscription", started_at=now)
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)

        page = render_results_page(now=now)

        assert now.date().isoformat() in page.splitlines()[0]
        assert "MEASURED NOTHING" in page
        assert "0 passed" not in page

    def test_stale_measured_run_lists_failing_scenarios(self) -> None:
        now = timezone.now()
        run = EvalRunRecord.objects.create(model="subscription", started_at=now - timedelta(days=8))
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.FAIL, cost_usd=0.25)

        page = render_results_page(now=now)

        assert "STALE:" in page
        assert "alpha" in page
        assert "0 passed" in page
        assert "1 failed" in page
