"""Governor gating of the HEADLESS admission chokepoints (#3644 / F9).

The pure decision (``decide_admission``) is exercised by
``test_admission_governor``; these cover the CORE-side wiring that F9 added:
the ``headless_admission_denied_reason`` seam and its consultation at the
post_save auto-enqueue and the drain safety net, so a governor DENY refuses a
new headless admission with a VISIBLE log instead of silently admitting into
the measured congestion collapse.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, override_settings

from teatree.core import headless_admission as gate_mod
from teatree.core.admission_governor import MachineSignal, QuotaSignal
from teatree.core.headless_admission import headless_admission_denied_reason
from teatree.core.models import Session, Task, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

IMMEDIATE_BACKEND = {"TASKS": {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}}}

_WEEK = 7 * 24 * 3600


def _healthy_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=False,
        weekly_utilization=0.1,
        short_utilization=0.1,
        seconds_to_weekly_reset=_WEEK * 0.5,
    )


def _exhausted_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=True,
        weekly_utilization=1.0,
        short_utilization=1.0,
        seconds_to_weekly_reset=100.0,
    )


def _machine(load1: float = 1.0) -> MachineSignal:
    return MachineSignal(cores=8, load1=load1, ram_available_gb=20.0)


class TestHeadlessAdmissionDeniedReason(TestCase):
    def test_kill_switch_off_admits(self) -> None:
        with patch.object(gate_mod, "governor_enabled", return_value=False):
            assert headless_admission_denied_reason() is None

    def test_a_signal_read_failure_admits_fail_open(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", side_effect=RuntimeError("probe down")),
        ):
            assert headless_admission_denied_reason() is None

    def test_quota_exhaustion_denies_with_a_reason(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_exhausted_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine()),
        ):
            reason = headless_admission_denied_reason()
        assert reason is not None
        assert "quota-exhausted" in reason

    def test_healthy_signals_below_ceiling_admit(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine()),
        ):
            assert headless_admission_denied_reason() is None

    def test_live_count_at_ceiling_denies(self) -> None:
        # A healthy quota yields a positive ceiling; a live headless-agent count
        # at/over it is the backpressure the interactive lane already had.
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", return_value=_healthy_quota()),
            patch.object(gate_mod, "read_machine_signal", return_value=_machine()),
            patch.object(Task.objects, "live_headless_agent_count", return_value=999),
        ):
            reason = headless_admission_denied_reason()
        assert reason is not None
        assert "at/over governor ceiling" in reason


class TestDrainConsultsTheGovernor(TestCase):
    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415 - deferred: local import

        from teatree.core.signals import _auto_enqueue_headless_task  # noqa: PLC0415 - deferred: local import

        post_save.disconnect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
        self.addCleanup(
            post_save.connect, _auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless"
        )

    def _pending_headless(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
        )

    @override_settings(**IMMEDIATE_BACKEND)
    def test_drain_admits_nothing_on_a_governor_deny(self) -> None:
        from teatree.core.tasks import drain_headless_queue_body  # noqa: PLC0415 - deferred: local import

        task = self._pending_headless()
        with patch.object(gate_mod, "headless_admission_denied_reason", return_value="weekly window spent"):
            result = drain_headless_queue_body()

        assert result["enqueued"] == []
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    @override_settings(**IMMEDIATE_BACKEND)
    def test_drain_admits_when_the_governor_is_silent(self) -> None:
        from teatree.core.tasks import drain_headless_queue_body  # noqa: PLC0415 - deferred: local import

        task = self._pending_headless()
        with (
            patch.object(gate_mod, "headless_admission_denied_reason", return_value=None),
            patch("teatree.core.tasks.execute_headless_task") as enqueue_task,
        ):
            enqueue_task.enqueue = MagicMock()
            result = drain_headless_queue_body()

        assert task.pk in result["enqueued"]


class TestAutoEnqueueConsultsTheGovernor(TestCase):
    @override_settings(**IMMEDIATE_BACKEND)
    def test_auto_enqueue_is_suppressed_on_a_governor_deny(self) -> None:
        # The post_save auto-enqueue must consult the governor: a DENY leaves the
        # task PENDING for the (also-gated) drain, never fires the dispatch.
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        with (
            patch.object(gate_mod, "headless_admission_denied_reason", return_value="load over watermark"),
            patch("teatree.core.tasks.execute_headless_task") as enqueue_task,
        ):
            enqueue_task.enqueue = MagicMock()
            Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                status=Task.Status.PENDING,
                phase="architectural_review",
            )
            enqueue_task.enqueue.assert_not_called()
