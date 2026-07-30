"""DB-backed tests for the shared ``PhaseCadence`` cadence-scanner helper.

``PhaseCadence`` owns the dedupe / last-run / bootstrap-cadence / queue-task
machinery the periodic task-queuing scanners (``eval_local``, ``scanning_news``,
``architectural_review``, ``backlog_sweep``, ``triage_assessor``,
``provision_smoke``) share. Each scanner's end-to-end behaviour is pinned in its
own ``tests/teatree_loop/test_*_scanner.py``; this file pins the seam directly,
including the model-not-migrated degradation branches.
"""

import datetime as dt
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.phase_cadence import PhaseCadence

OVERLAY = "t3-teatree"
PHASE = "eval_local"


def _cadence(*, overlay: str = OVERLAY, phase: str = PHASE, cadence_hours: int = 168) -> PhaseCadence:
    return PhaseCadence(overlay, phase, cadence_hours)


class InFlightExistsTests(TestCase):
    def test_true_when_pending_task_present(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY)
        session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase=PHASE, status=Task.Status.PENDING)

        assert _cadence().in_flight_exists() is True

    def test_false_when_only_terminal_tasks(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY)
        session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase=PHASE, status=Task.Status.COMPLETED)

        assert _cadence().in_flight_exists() is False

    def test_false_when_task_model_missing(self) -> None:
        with patch("teatree.loop.scanners.phase_cadence._task_model", return_value=None):
            assert _cadence().in_flight_exists() is False


class LastRunAtTests(TestCase):
    def test_none_when_no_task(self) -> None:
        assert _cadence().last_run_at() is None

    def test_none_when_task_model_missing(self) -> None:
        with patch("teatree.loop.scanners.phase_cadence._task_model", return_value=None):
            assert _cadence().last_run_at() is None

    def test_last_completed_run_at_ignores_a_newer_failed_task(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY)
        completed_session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        Session.objects.filter(pk=completed_session.pk).update(started_at=timezone.now() - timedelta(hours=200))
        Task.objects.create(ticket=ticket, session=completed_session, phase=PHASE, status=Task.Status.COMPLETED)
        failed_session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=failed_session, phase=PHASE, status=Task.Status.FAILED)

        # The success clock ignores the newer FAILED task; the backoff clock counts it.
        completed_run = _cadence().last_completed_run_at()
        terminal_run = _cadence().last_terminal_run_at()

        assert completed_run is not None
        assert (timezone.now() - completed_run) > timedelta(hours=100)
        assert terminal_run is not None
        assert (timezone.now() - terminal_run) < timedelta(hours=2)

    def test_clock_helpers_none_when_task_model_missing(self) -> None:
        with patch("teatree.loop.scanners.phase_cadence._task_model", return_value=None):
            assert _cadence().last_completed_run_at() is None
            assert _cadence().last_terminal_run_at() is None


class EvaluateTriggerTests(TestCase):
    NOW = dt.datetime(2026, 6, 16, 12, 0, tzinfo=dt.UTC)

    def test_bootstrap_when_never_run(self) -> None:
        assert _cadence().evaluate_trigger(now=self.NOW, last_run_at=None) == "bootstrap"

    def test_cadence_when_elapsed(self) -> None:
        last = self.NOW - timedelta(hours=169)
        assert _cadence(cadence_hours=168).evaluate_trigger(now=self.NOW, last_run_at=last) == "cadence"

    def test_none_when_within_window(self) -> None:
        last = self.NOW - timedelta(hours=24)
        assert _cadence(cadence_hours=168).evaluate_trigger(now=self.NOW, last_run_at=last) is None


class QueueTaskTests(TestCase):
    def _queue(self) -> Task | None:
        return _cadence().queue_task(
            placeholder_issue_url=f"eval-local://{OVERLAY}",
            agent_id=f"eval-local-{OVERLAY}",
            execution_reason="reason",
            log_label="Test",
        )

    def test_creates_task_anchored_at_placeholder_ticket(self) -> None:
        task = self._queue()

        assert task is not None
        assert task.phase == PHASE
        assert task.status == Task.Status.PENDING
        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.ticket.overlay == OVERLAY
        assert task.ticket.issue_url == f"eval-local://{OVERLAY}"

    def test_reanchors_placeholder_ticket_to_target_overlay(self) -> None:
        Ticket.objects.create(issue_url=f"eval-local://{OVERLAY}", overlay="stale-overlay", role="author")

        task = self._queue()

        assert task is not None
        assert Ticket.objects.get(issue_url=f"eval-local://{OVERLAY}").overlay == OVERLAY

    def test_returns_none_when_a_model_is_missing(self) -> None:
        with patch("teatree.loop.scanners.phase_cadence._ticket_model", return_value=None):
            assert self._queue() is None

    def test_swallows_write_errors_and_returns_none(self) -> None:
        with patch.object(Task.objects, "create", side_effect=RuntimeError("db down")):
            assert self._queue() is None
        assert not Task.objects.filter(phase=PHASE).exists()
