"""Health reads the recorded local eval rather than an agent's reported success."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.eval_measurement import eval_measurement_signal
from teatree.core.models import EvalRunRecord, EvalVerdict, Loop


class TestEvalMeasurement(TestCase):
    def test_latest_unmeasured_run_warns(self) -> None:
        now = timezone.now()
        run = EvalRunRecord.objects.create(model="subscription", started_at=now)
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)

        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            signal = eval_measurement_signal(now)

        assert signal is not None
        assert signal.severity == "warning"
        assert "measured nothing" in signal.summary

    def test_old_run_without_new_measurement_is_critical(self) -> None:
        now = timezone.now()
        Loop.objects.filter(name="eval_local").update(delay_seconds=86400)
        run = EvalRunRecord.objects.create(model="subscription", started_at=now - timedelta(days=16))
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.FAIL)

        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            signal = eval_measurement_signal(now)

        assert signal is not None
        assert signal.severity == "critical"

    def test_fresh_install_and_disabled_loop_have_no_signal(self) -> None:
        now = timezone.now()
        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            assert eval_measurement_signal(now) is None
        run = EvalRunRecord.objects.create(model="subscription", started_at=now)
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)
        with patch("teatree.loops.enable_verdict.loop_admits", return_value=False):
            assert eval_measurement_signal(now) is None
