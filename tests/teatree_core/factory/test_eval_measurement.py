"""Health reads the recorded local eval rather than an agent's reported success."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.eval_measurement import eval_measurement_signal
from teatree.core.factory.operational_health import collect_signals
from teatree.core.models import EvalRunRecord, EvalVerdict, Loop


class TestEvalMeasurement(TestCase):
    def test_latest_unmeasured_run_warns(self) -> None:
        now = timezone.now()
        run = EvalRunRecord.objects.create(model="subscription", suite="eval_local:1", started_at=now)
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)

        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            signal = eval_measurement_signal(now)

        assert signal is not None
        assert signal.severity == "warning"
        assert "measured nothing" in signal.summary

    def test_unrelated_newer_run_does_not_mask_a_local_failure(self) -> None:
        now = timezone.now()
        local = EvalRunRecord.objects.create(
            model="subscription", suite="eval_local:1", started_at=now - timedelta(minutes=1)
        )
        local.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)
        manual = EvalRunRecord.objects.create(model="manual", suite="other", started_at=now)
        manual.record_scenario(scenario_name="alpha", verdict=EvalVerdict.PASS)

        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            signal = eval_measurement_signal(now)

        assert signal is not None
        assert "measured nothing" in signal.summary

    def test_old_run_without_new_measurement_is_critical(self) -> None:
        now = timezone.now()
        Loop.objects.filter(name="eval_local").update(delay_seconds=86400)
        run = EvalRunRecord.objects.create(
            model="subscription", suite="eval_local:1", started_at=now - timedelta(days=16)
        )
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.FAIL)

        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            signal = eval_measurement_signal(now)

        assert signal is not None
        assert signal.severity == "critical"

    def test_fresh_install_and_disabled_loop_have_no_signal(self) -> None:
        now = timezone.now()
        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            assert eval_measurement_signal(now) is None
        run = EvalRunRecord.objects.create(model="subscription", suite="eval_local:1", started_at=now)
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)
        with patch("teatree.loops.enable_verdict.loop_admits", return_value=False):
            assert eval_measurement_signal(now) is None

    def test_operational_health_collects_the_local_failure(self) -> None:
        run = EvalRunRecord.objects.create(model="subscription", suite="eval_local:1")
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)

        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            collection = collect_signals()

        assert any(signal.kind == "eval_measurement" for signal in collection.signals)

    def test_operational_health_marks_an_unread_eval_source(self) -> None:
        with patch(
            "teatree.core.factory.operational_health.eval_measurement_signal", side_effect=RuntimeError("unread")
        ):
            collection = collect_signals()

        assert "_eval_measurement_signals" in collection.unread
