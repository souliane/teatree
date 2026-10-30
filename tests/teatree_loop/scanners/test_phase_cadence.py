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
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.operational_health import HealthStatus, read_health
from teatree.core.modelkit.phases import EVAL_LOCAL_PHASE
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.architectural_review import ARCHITECTURAL_REVIEW_PHASE, ArchitecturalReviewScanner
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.eval_local import EvalLocalScanner
from teatree.loop.scanners.phase_cadence import PhaseCadence
from teatree.loop.scanners.triage_assessor import TriageAssessorScanner

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

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


class OutcomeTests(TestCase):
    def test_gate_names_an_in_flight_task(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY)
        session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase=PHASE, status=Task.Status.CLAIMED)
        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now())

        trigger, signals = _cadence().gate(
            now=timezone.now(), held_kind="eval_local.held", not_due_kind="eval_local.not_due"
        )

        assert trigger is None
        assert signals[0].kind == "eval_local.not_due"
        assert "in flight" in signals[0].payload["reason"]

    def test_handover_reports_an_unadmitted_task_as_retryable(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY)
        session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase=PHASE)
        Task.objects.filter(pk=task.pk).update(admitted_at=None)
        queued = ScanSignal(kind="eval_local.queued", summary="queued", payload={"task_id": task.pk})

        with patch("teatree.loop.scanners.phase_cadence.pending_admission_reason", return_value="machine HALT"):
            signals = _cadence().handover(task, queued, held_kind="eval_local.held")

        assert signals[0].kind == "eval_local.held"
        assert signals[0].payload["reason"] == "machine HALT"
        assert signals[0].payload["retry"] is True

    def test_hold_names_reason_and_records_retryable_health(self) -> None:
        signals = _cadence().hold("reserve unmet", kind="eval_local.held", retry=True)

        assert len(signals) == 1
        assert signals[0].payload["reason"] == "reserve unmet"
        assert signals[0].payload["retry"] is True
        issue = KnownIssue.objects.get(fingerprint=f"cadence-held:{PHASE}:{OVERLAY}")
        assert issue.summary.endswith("reserve unmet")
        assert issue.auto_resolve is False

    def test_not_due_resolves_an_earlier_hold_and_writes_no_row(self) -> None:
        cadence = _cadence()
        cadence.hold("lane full", kind="eval_local.held", retry=True)

        signals = cadence.not_due("task running", kind="eval_local.not_due")

        assert signals[0].payload["reason"] == "task running"
        assert KnownIssue.objects.get(fingerprint=f"cadence-held:{PHASE}:{OVERLAY}").resolved_at is not None
        assert not KnownIssue.objects.open().exists()

    def test_an_idle_hold_resolves_an_earlier_abnormal_hold_and_writes_no_row(self) -> None:
        cadence = _cadence()
        cadence.hold("no scope", kind="eval_local.held", retry=False)

        signals = cadence.hold("batch empty", kind="eval_local.held", retry=False, record_health=False)

        assert signals[0].payload["retry"] is False
        assert KnownIssue.objects.get(fingerprint=f"cadence-held:{PHASE}:{OVERLAY}").resolved_at is not None
        assert not KnownIssue.objects.open().exists()

    def test_gate_holds_for_retry_while_an_in_flight_task_waits_for_admission(self) -> None:
        ticket = Ticket.objects.create(overlay=OVERLAY)
        session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase=PHASE, status=Task.Status.PENDING)
        Task.objects.filter(pk=task.pk).update(admitted_at=None)

        with patch("teatree.loop.scanners.phase_cadence.pending_admission_reason", return_value="machine HALT"):
            trigger, signals = _cadence().gate(
                now=timezone.now(), held_kind="eval_local.held", not_due_kind="eval_local.not_due"
            )

        assert trigger is None
        assert signals[0].kind == "eval_local.held"
        assert signals[0].payload["reason"] == "machine HALT"
        assert signals[0].payload["retry"] is True
        assert signals[0].payload["task_id"] == task.pk
        assert KnownIssue.objects.open().filter(fingerprint=f"cadence-held:{PHASE}:{OVERLAY}").exists()

    def test_handover_without_a_task_holds_for_retry(self) -> None:
        queued = ScanSignal(kind="eval_local.queued", summary="queued", payload={})

        signals = _cadence().handover(None, queued, held_kind="eval_local.held")

        assert signals[0].kind == "eval_local.held"
        assert signals[0].payload["reason"] == "task was not created"
        assert signals[0].payload["retry"] is True
        assert KnownIssue.objects.open().filter(fingerprint=f"cadence-held:{PHASE}:{OVERLAY}").exists()

    def test_handover_of_an_admitted_task_clears_an_earlier_hold(self) -> None:
        cadence = _cadence()
        cadence.hold("lane full", kind="eval_local.held", retry=True)
        ticket = Ticket.objects.create(overlay=OVERLAY)
        session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase=PHASE)
        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now())
        queued = ScanSignal(kind="eval_local.queued", summary="queued", payload={"task_id": task.pk})

        signals = cadence.handover(task, queued, held_kind="eval_local.held")

        assert signals == [queued]
        assert not KnownIssue.objects.open().exists()


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

        # The cadence clock ignores the newer FAILED task.
        completed_run = _cadence().last_completed_run_at()

        assert completed_run is not None
        assert (timezone.now() - completed_run) > timedelta(hours=100)

    def test_clock_helpers_none_when_task_model_missing(self) -> None:
        with patch("teatree.loop.scanners.phase_cadence._task_model", return_value=None):
            assert _cadence().last_completed_run_at() is None


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


def _task_for(phase: str, status: Task.Status, *, started_hours_ago: float = 0.0) -> Task:
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=f"{phase}://{OVERLAY}", defaults={"overlay": OVERLAY, "role": "author"}
    )
    session = Session.objects.create(overlay=OVERLAY, ticket=ticket, agent_id="a")
    Session.objects.filter(pk=session.pk).update(started_at=timezone.now() - timedelta(hours=started_hours_ago))
    task = Task.objects.create(ticket=ticket, session=session, phase=phase, status=status)
    Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now())
    return task


class NormalStatesKeepHealthGreenTests(TestCase):
    def test_not_due_task_running_and_empty_batch_leave_the_chip_green(self) -> None:
        _task_for(ARCHITECTURAL_REVIEW_PHASE, Task.Status.COMPLETED, started_hours_ago=13)
        _task_for(EVAL_LOCAL_PHASE, Task.Status.CLAIMED)
        host = Mock()
        host.list_assigned_issues.return_value = []
        host.list_labeled_issues.return_value = []

        signals = [
            *ArchitecturalReviewScanner(overlay_name=OVERLAY).scan(),
            *EvalLocalScanner(overlay_name=OVERLAY).scan(),
            *TriageAssessorScanner(
                host=cast("CodeHostBackend", host),
                overlay_name=OVERLAY,
                identities=("operator",),
                repo_slugs=("souliane/teatree",),
            ).scan(),
        ]

        assert [signal.kind for signal in signals] == [
            "architectural_review.not_due",
            "eval_local.not_due",
            "triage_assessor.held",
        ]
        assert all(signal.payload["reason"] for signal in signals)
        assert read_health().status == HealthStatus.GREEN
