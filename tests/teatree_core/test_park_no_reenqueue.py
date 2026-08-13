"""A usage-window park quiesces the lane — it never re-arms the dispatch it just refused.

``Task.park`` returns the task to the queue PENDING with a future ``not_before``, and that
save fires the ``post_save`` auto-enqueue. Before the fix the signal read only
``status == PENDING`` and immediately enqueued a fresh ``execute_task`` for a task
its own claim CAS refuses — a self-feeding edge that, before the claim gained the same
``not_before`` predicate, spun at the worker round-trip (a measured 47,172 park rows on one
task in eight hours). The drain (``_claimable_now_q``) and the claim CAS both honour the
park gate; this pins the third seam.
"""

import datetime as dt
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.agents.usage_window import maybe_park_for_active_window
from teatree.core.agent_admission import AgentAdmission
from teatree.core.managers import ADMITTED_INFLIGHT_WINDOW
from teatree.core.managers_task_claim import _claimable_now_q
from teatree.core.models import LIMIT_PARKED_PREFIX, Session, Task, TaskAttempt, Ticket, UsageWindowState
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.task_claim import window_parked

#: A phase with no registered agent, so the headless auto-enqueue seam stays live under the
#: default interactive runtime (a loop-dispatched phase would short-circuit the signal).
_FREE_FORM_PHASE = "architectural_review"
_LANE = TaskAttempt.Lane.SUBSCRIPTION


class TestParkDoesNotReEnqueue(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)
        self.admit = mock.patch(
            "teatree.core.agent_admission.agent_admission_verdict",
            return_value=AgentAdmission(expensive_denied=None, cheap_denied=None),
        )
        self.admit.start()
        self.addCleanup(self.admit.stop)
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/1", role=Ticket.Role.AUTHOR)
        self.session = Session.objects.create(ticket=self.ticket)
        self.resets_at = timezone.now() + dt.timedelta(hours=5)

    def _dispatching_task(self) -> Task:
        """A headless task mid-dispatch — CLAIMED, exactly as the admission guard finds it."""
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            phase=_FREE_FORM_PHASE,
        )
        Task.objects.filter(pk=task.pk).update(status=Task.Status.CLAIMED)
        task.refresh_from_db()
        return task

    def _exhausted_lane(self) -> UsageWindowState:
        return UsageWindowState.record_limit(
            lane=_LANE,
            cause="all_accounts_exhausted",
            resets_at=self.resets_at,
        )

    def test_park_does_not_re_arm_the_agent_runner(self) -> None:
        window = self._exhausted_lane()
        task = self._dispatching_task()
        with mock.patch("teatree.core.tasks.execute_task") as job:
            attempt = maybe_park_for_active_window(task, lane=_LANE)
        assert job.enqueue.call_count == 0
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == window.resets_at
        # The park stays visible: one attempt row naming the lane and the reason.
        assert attempt is not None
        assert attempt.error.startswith(LIMIT_PARKED_PREFIX)
        assert TaskAttempt.objects.filter(task=task).count() == 1

    def test_repeated_park_stays_one_visible_row(self) -> None:
        self._exhausted_lane()
        task = self._dispatching_task()
        with mock.patch("teatree.core.tasks.execute_task"):
            maybe_park_for_active_window(task, lane=_LANE)
            maybe_park_for_active_window(task, lane=_LANE)
        rows = TaskAttempt.objects.filter(task=task)
        assert rows.count() == 1
        assert rows.get().park_repeats == 1

    def test_unparked_headless_task_is_still_enqueued(self) -> None:
        # Control: the guard is scoped to the park gate, not a blanket disable of the lane.
        with mock.patch("teatree.core.tasks.execute_task") as job:
            task = Task.objects.create(
                ticket=self.ticket,
                session=self.session,
                phase=_FREE_FORM_PHASE,
            )
        job.enqueue.assert_called_once_with(task.pk, task.phase)

    def test_elapsed_park_gate_re_enqueues(self) -> None:
        # Control: once the window re-arms, an ordinary PENDING save enqueues again, so the
        # suppression is keyed on the FUTURE instant rather than on the field being set.
        task = self._dispatching_task()
        task.park(not_before=timezone.now() - dt.timedelta(minutes=1))
        # A re-arm is a LATER instant, so the row's admission seat has aged out by then too.
        # Within ADMITTED_INFLIGHT_WINDOW the re-enqueue is a duplicate dispatch (#4125), which
        # is a second suppression this control is not measuring.
        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now() - ADMITTED_INFLIGHT_WINDOW)
        with mock.patch("teatree.core.tasks.execute_task") as job:
            task.save(update_fields=["status", "not_before"])
        job.enqueue.assert_called_once_with(task.pk, task.phase)


class TestWindowParkedPredicateParity(TestCase):
    """All THREE spellings of the park gate answer identically (no drift).

    ``window_parked`` is the shared predicate; ``Task.is_window_parked`` delegates to it and
    ``_claimable_now_q`` is its queryset form. A row every spelling does not agree on is a
    seam where "is there work" and "may this be re-dispatched" could diverge.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/2", role=Ticket.Role.AUTHOR)
        self.session = Session.objects.create(ticket=self.ticket)

    def test_every_spelling_of_the_park_gate_agrees(self) -> None:
        now = timezone.now()
        gates = [None, now - dt.timedelta(minutes=1), now + dt.timedelta(minutes=1)]
        tasks = [
            Task.objects.create(ticket=self.ticket, session=self.session, phase=_FREE_FORM_PHASE, not_before=gate)
            for gate in gates
        ]
        claimable = set(Task.objects.filter(_claimable_now_q(now)).values_list("pk", flat=True))
        for task in tasks:
            parked = task.pk not in claimable
            assert window_parked(task, now) is parked
            assert task.is_window_parked(now) is parked
