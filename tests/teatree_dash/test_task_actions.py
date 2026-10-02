"""The drawer's enqueue-button read model: which phases, enabled, and why not (#4085)."""

import pytest
from django.test import TestCase

from teatree.core.modelkit.phases import CANONICAL_PHASES
from teatree.core.models.task import Task
from teatree.core.models.task_enqueue import DuplicatePhaseTaskError, enqueue_phase_task_once
from teatree.core.models.ticket import Ticket
from teatree.dash.task_actions import (
    BOARD_PHASES,
    ENQUEUEABLE_PHASES,
    board_enqueue_buttons,
    enqueue_buttons,
    pending_phase_tasks_by_ticket,
)
from tests.factories import TaskFactory


class EnqueueablePhaseVocabularyTestCase(TestCase):
    def test_every_offered_phase_is_a_canonical_phase(self) -> None:
        assert set(ENQUEUEABLE_PHASES) <= CANONICAL_PHASES

    def test_the_two_phases_the_owner_asked_for_are_offered(self) -> None:
        assert "reviewing" in ENQUEUEABLE_PHASES
        assert "shipping" in ENQUEUEABLE_PHASES


class EnqueueButtonsTestCase(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test")

    def test_one_button_per_offered_phase_in_that_order(self) -> None:
        buttons = enqueue_buttons(self.ticket)
        assert tuple(button.phase for button in buttons) == ENQUEUEABLE_PHASES

    def test_every_button_is_enabled_with_no_reason_when_nothing_is_queued(self) -> None:
        for button in enqueue_buttons(self.ticket):
            assert button.enabled
            assert button.reason == ""

    def test_the_label_reads_as_a_now_verb(self) -> None:
        labels = {button.phase: button.label for button in enqueue_buttons(self.ticket)}
        assert labels["reviewing"] == "Review now"
        assert labels["shipping"] == "Ship now"

    def test_a_pending_task_disables_its_own_phase_and_names_the_task(self) -> None:
        task = TaskFactory(ticket=self.ticket, phase="reviewing", status=Task.Status.PENDING)
        buttons = {button.phase: button for button in enqueue_buttons(self.ticket)}
        assert not buttons["reviewing"].enabled
        assert f"TODO-{task.pk}" in buttons["reviewing"].reason
        assert buttons["shipping"].enabled

    def test_a_claimed_task_leaves_its_phase_enqueueable(self) -> None:
        TaskFactory(ticket=self.ticket, phase="reviewing", status=Task.Status.CLAIMED)
        buttons = {button.phase: button for button in enqueue_buttons(self.ticket)}
        assert buttons["reviewing"].enabled

    def test_the_disabled_button_names_the_oldest_task_the_post_would_refuse_against(self) -> None:
        """The drawer half of the guarantee the board sibling already pins (#4271).

        Flip the ordering in ``_oldest_pending_task_per_phase`` and the button names the
        newest task while the POST still refuses against the oldest — the exact
        disagreement its docstring says can never happen.
        """
        oldest = TaskFactory(ticket=self.ticket, phase="shipping", status=Task.Status.PENDING)
        TaskFactory(ticket=self.ticket, phase="shipping", status=Task.Status.PENDING)
        buttons = {button.phase: button for button in enqueue_buttons(self.ticket)}
        with pytest.raises(DuplicatePhaseTaskError) as refusal:
            enqueue_phase_task_once(ticket=self.ticket, phase="shipping", reason="Ship now.")

        assert not buttons["shipping"].enabled
        assert buttons["shipping"].reason == f"TODO-{oldest.pk} is already queued for shipping"
        assert f"TODO-{oldest.pk} " in str(refusal.value)

    def test_the_button_set_costs_one_query_whatever_the_task_count(self) -> None:
        for scale in (2, 20):
            for index in range(scale):
                TaskFactory(ticket=self.ticket, phase=f"free-form-{index}", status=Task.Status.PENDING)
            with self.assertNumQueries(1, msg=f"at scale {scale}"):
                enqueue_buttons(self.ticket)


class BoardEnqueueButtonsTestCase(TestCase):
    """The card's two buttons — the same read model, batched across the whole board."""

    def setUp(self) -> None:
        self.tickets = [Ticket.objects.create(overlay="test") for _ in range(3)]

    def test_the_card_offers_only_the_two_phases_the_owner_asked_for(self) -> None:
        buttons = board_enqueue_buttons({})
        assert tuple(button.phase for button in buttons) == BOARD_PHASES
        assert BOARD_PHASES == ("reviewing", "shipping")

    def test_a_queued_phase_arrives_disabled_naming_its_task(self) -> None:
        buttons = {button.phase: button for button in board_enqueue_buttons({"reviewing": 7})}
        assert not buttons["reviewing"].enabled
        assert "TODO-7" in buttons["reviewing"].reason
        assert buttons["shipping"].enabled

    def test_the_whole_board_costs_one_query_whatever_the_ticket_count(self) -> None:
        for scale in (1, 3):
            ids = [ticket.pk for ticket in self.tickets[:scale]]
            with self.assertNumQueries(1, msg=f"at scale {scale}"):
                pending_phase_tasks_by_ticket(ids)

    def test_it_maps_each_ticket_to_its_own_pending_phases(self) -> None:
        first, second, _third = self.tickets
        task = TaskFactory(ticket=first, phase="reviewing", status=Task.Status.PENDING)
        TaskFactory(ticket=second, phase="reviewing", status=Task.Status.CLAIMED)
        queued = pending_phase_tasks_by_ticket([t.pk for t in self.tickets])
        assert queued == {first.pk: {"reviewing": task.pk}}

    def test_an_empty_board_reads_nothing(self) -> None:
        with self.assertNumQueries(0):
            assert pending_phase_tasks_by_ticket([]) == {}

    def test_it_names_the_oldest_pending_task_the_post_would_refuse_against(self) -> None:
        ticket = self.tickets[0]
        oldest = TaskFactory(ticket=ticket, phase="shipping", status=Task.Status.PENDING)
        TaskFactory(ticket=ticket, phase="shipping", status=Task.Status.PENDING)
        assert pending_phase_tasks_by_ticket([ticket.pk])[ticket.pk]["shipping"] == oldest.pk
