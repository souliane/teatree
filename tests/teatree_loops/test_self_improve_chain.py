"""The unattended worker runs the cheap monitor even without a Claude session."""

from typing import TextIO, cast
from unittest import mock

from django.tasks import TaskResultStatus
from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.schedule import TierResult
from teatree.loops import self_improve_cycle, timer_reconciler
from teatree.loops.timer_reconciler import ensure_maintenance_chains
from tests._t3_master_env import worker_owns_t3_master

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}


@override_settings(TASKS=_DB_TASKS)
class TestSelfImproveChain(TestCase):
    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def test_worker_seeds_one_unattended_monitor_head(self) -> None:
        ensure_maintenance_chains()
        ensure_maintenance_chains()
        rows = DBTaskResult.objects.filter(task_path=timer_reconciler.run_self_improve.module_path)
        assert rows.count() == 1
        assert rows.get().queue_name == "loops"
        assert rows.get().run_after > timezone.now()  # preserve the existing cadence seed

    def test_cycle_calls_existing_lease_guarded_command_and_rearms(self) -> None:
        def emit_result(*_args: object, **kwargs: object) -> None:
            cast("TextIO", kwargs["stdout"]).write('{"skipped": false}')

        with mock.patch.object(self_improve_cycle, "call_command", side_effect=emit_result) as command:
            result = timer_reconciler.run_self_improve.func()
        assert result == {"ran": 1}
        command.assert_called_once()
        assert command.call_args.args == ("loop_self_improve",)
        assert command.call_args.kwargs["tier"] == "cheap"
        assert (
            DBTaskResult.objects.filter(
                task_path=timer_reconciler.run_self_improve.module_path,
                status=TaskResultStatus.READY,
            ).count()
            == 1
        )

    def test_degraded_scan_is_warning_and_result_not_silent_success(self) -> None:
        def emit_result(*_args: object, **kwargs: object) -> None:
            cast("TextIO", kwargs["stdout"]).write(
                '{"skipped": false, "degraded_scans": [{"detector": "pressure_incident", "reason": "otel_missing"}]}'
            )

        with (
            mock.patch.object(self_improve_cycle, "call_command", side_effect=emit_result),
            mock.patch.object(self_improve_cycle.logger, "warning") as warning,
        ):
            result = timer_reconciler.run_self_improve.func()

        assert result == {"ran": 1, "degraded": 1}
        warning.assert_called_once()
        assert "pressure_incident" in str(warning.call_args)
        assert "otel_missing" in str(warning.call_args)

    def test_cycle_dedups_pending_successor(self) -> None:
        timer_reconciler.run_self_improve.enqueue()
        with mock.patch.object(self_improve_cycle, "call_command") as command:
            assert timer_reconciler.run_self_improve.func() == {"deduped": 1}
        command.assert_not_called()

    def test_unattended_cycle_reaches_command_with_worker_ownership(self) -> None:
        self.enterContext(worker_owns_t3_master())
        result = TierResult(tier="cheap", budget=BudgetVerdict.allow())
        with mock.patch("teatree.loop.self_improve.schedule.run_tier", return_value=result) as run_tier:
            assert timer_reconciler.run_self_improve.func() == {"ran": 1}
        run_tier.assert_called_once()

    def test_unclaimed_master_is_visible_as_skip_not_success(self) -> None:
        assert timer_reconciler.run_self_improve.func() == {"skipped": 1}
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_self_improve.module_path).count() == 1

    def test_budget_skip_is_not_reported_as_a_successful_scan(self) -> None:
        def emit_result(*_args: object, **kwargs: object) -> None:
            cast("TextIO", kwargs["stdout"]).write('{"skipped": true, "budget_reason": "quota"}')

        with mock.patch.object(self_improve_cycle, "call_command", side_effect=emit_result):
            assert timer_reconciler.run_self_improve.func() == {"skipped": 1}

    def test_failed_cycle_keeps_its_successor(self) -> None:
        with mock.patch.object(self_improve_cycle, "call_command", side_effect=RuntimeError("probe failed")):
            assert timer_reconciler.run_self_improve.func() == {"error": 1}
        assert DBTaskResult.objects.filter(task_path=timer_reconciler.run_self_improve.module_path).count() == 1
