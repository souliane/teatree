"""The one phase-task create seam shared by ``tasks create`` and the dashboard (#4085)."""

import pytest
from django.test import TestCase

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.task_enqueue import (
    DuplicatePhaseTaskError,
    TaskEnqueueError,
    enqueue_phase_task,
    enqueue_phase_task_once,
)


class EnqueuePhaseTaskTestCase(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test")

    def test_it_creates_a_pending_task_on_the_canonical_phase_session(self) -> None:
        task = enqueue_phase_task(ticket=self.ticket, phase="scoping", reason="Decide X.")
        assert task.ticket_id == self.ticket.pk
        assert task.phase == "scoping"
        assert task.execution_reason == "Decide X."
        assert task.status == Task.Status.PENDING
        assert task.session_id == self.ticket.sessions.order_by("pk").first().pk

    def test_it_reuses_the_earliest_existing_session(self) -> None:
        existing = Session.objects.create(ticket=self.ticket, overlay="test")
        task = enqueue_phase_task(ticket=self.ticket, phase="scoping", reason="Decide X.")
        assert task.session_id == existing.pk

    def test_the_interactive_flag_selects_the_interactive_lane(self) -> None:
        task = enqueue_phase_task(ticket=self.ticket, phase="scoping", reason="x", interactive=True)
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE

    def test_a_blank_phase_is_refused(self) -> None:
        with pytest.raises(TaskEnqueueError):
            enqueue_phase_task(ticket=self.ticket, phase="   ", reason="x")
        assert not Task.objects.exists()

    def test_a_blank_reason_is_refused(self) -> None:
        with pytest.raises(TaskEnqueueError):
            enqueue_phase_task(ticket=self.ticket, phase="scoping", reason="   ")
        assert not Task.objects.exists()

    def test_the_plain_seam_does_not_refuse_a_duplicate(self) -> None:
        # The CLI path must stay byte-identical: the loop's phase handoff enqueues
        # freely, so only the dashboard's ``_once`` sibling carries the guard.
        enqueue_phase_task(ticket=self.ticket, phase="coding", reason="first")
        enqueue_phase_task(ticket=self.ticket, phase="coding", reason="second")
        assert Task.objects.filter(ticket=self.ticket, phase="coding").count() == 2


class EnqueueOnceTestCase(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test")

    def test_it_enqueues_when_nothing_is_queued_for_that_phase(self) -> None:
        task = enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review now.")
        assert task.phase == "reviewing"
        assert Task.objects.filter(ticket=self.ticket, phase="reviewing").count() == 1

    def test_a_second_enqueue_is_refused_while_the_first_is_unstarted(self) -> None:
        first = enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review now.")
        with pytest.raises(DuplicatePhaseTaskError) as exc:
            enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review now.")
        assert f"TODO-{first.pk}" in str(exc.value)
        assert Task.objects.filter(ticket=self.ticket, phase="reviewing").count() == 1

    def test_another_phase_is_unaffected_by_a_pending_task(self) -> None:
        enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review now.")
        enqueue_phase_task_once(ticket=self.ticket, phase="shipping", reason="Ship now.")
        assert Task.objects.filter(ticket=self.ticket).count() == 2

    def test_a_finished_task_does_not_block_a_re_enqueue(self) -> None:
        first = enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review now.")
        Task.objects.filter(pk=first.pk).update(status=Task.Status.COMPLETED)
        enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review again.")
        assert Task.objects.filter(ticket=self.ticket, phase="reviewing").count() == 2

    def test_a_claimed_task_does_not_block_a_re_enqueue(self) -> None:
        # A claimed task is being worked right now; blocking on it would leave an
        # operator unable to re-queue a phase whose worker died.
        first = enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review now.")
        Task.objects.filter(pk=first.pk).update(status=Task.Status.CLAIMED)
        enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="Review again.")
        assert Task.objects.filter(ticket=self.ticket, phase="reviewing").count() == 2

    def test_the_refusal_names_the_oldest_queued_task(self) -> None:
        # The disabled button names the oldest too, so the two can never disagree.
        first = enqueue_phase_task(ticket=self.ticket, phase="reviewing", reason="a")
        enqueue_phase_task(ticket=self.ticket, phase="reviewing", reason="b")
        with pytest.raises(DuplicatePhaseTaskError) as exc:
            enqueue_phase_task_once(ticket=self.ticket, phase="reviewing", reason="c")
        assert f"TODO-{first.pk}" in str(exc.value)
