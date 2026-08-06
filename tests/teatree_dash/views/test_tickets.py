"""Ticket drawer, the legal-only FSM-transition POST, and the phase-enqueue POST (#3162, #4085)."""

from django.http import HttpResponse
from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.dash.task_actions import enqueue_buttons
from teatree.dash.ticket_detail import legal_transition_names
from tests.factories import TaskFactory, TicketFactory

State = Ticket.State


class LegalTransitionSetTestCase(TestCase):
    def test_only_legal_transitions_are_offered(self) -> None:
        ticket = TicketFactory(state=State.NOT_STARTED)
        names = legal_transition_names(ticket)
        assert "scope" in names
        # ship is illegal from NOT_STARTED — it must not be offered.
        assert "ship" not in names


class TicketTransitionPostTestCase(TestCase):
    def setUp(self) -> None:
        self.ticket = TicketFactory(state=State.NOT_STARTED)
        self.url = reverse("dash:ticket_transition", args=[self.ticket.pk])

    def test_legal_transition_advances_state_via_model_method(self) -> None:
        self.client.post(self.url, {"action": "scope"})
        self.ticket.refresh_from_db()
        assert self.ticket.state == State.SCOPED
        # the guarded method fired, so the post_transition signal recorded a row.
        assert TicketTransition.objects.filter(ticket=self.ticket, to_state=State.SCOPED).exists()

    def test_illegal_transition_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"action": "ship"})
        assert resp.status_code == 400
        self.ticket.refresh_from_db()
        assert self.ticket.state == State.NOT_STARTED

    def test_unknown_action_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"action": "teleport"})
        assert resp.status_code == 400

    def test_transition_is_audited(self) -> None:
        with self.assertLogs("teatree.dash.audit", level="INFO") as logs:
            self.client.post(self.url, {"action": "scope"})
        assert any("action=ticket:scope" in line for line in logs.output)

    def test_csrf_is_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.post(self.url, {"action": "scope"})
        assert resp.status_code == 403


class TicketDrawerGetTestCase(TestCase):
    def test_drawer_renders_history_mermaid_and_actions(self) -> None:
        ticket = TicketFactory(state=State.STARTED, short_description="drawer subject")
        TicketTransition.objects.create(
            ticket=ticket, from_state=State.SCOPED, to_state=State.STARTED, triggered_by="start"
        )
        resp = self.client.get(reverse("dash:ticket_drawer", args=[ticket.pk]))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "drawer subject" in body
        assert "stateDiagram-v2" in body
        assert "start" in body

    def test_drawer_404_for_missing_ticket(self) -> None:
        resp = self.client.get(reverse("dash:ticket_drawer", args=[999999]))
        assert resp.status_code == 404

    def test_transition_buttons_carry_confirmation_naming_state(self) -> None:
        # #3264: a transition button must prompt before firing the FSM POST, and the
        # prompt must name the ticket + the target action so an accidental click is caught.
        ticket = TicketFactory(state=State.NOT_STARTED)
        body = self.client.get(reverse("dash:ticket_drawer", args=[ticket.pk])).content.decode()
        assert "return confirm(" in body
        assert f"Transition #{ticket.ticket_number} to scope?" in body

    def test_debug_session_button_carries_confirmation(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        body = self.client.get(reverse("dash:ticket_drawer", args=[ticket.pk])).content.decode()
        assert "hx-confirm=" in body
        assert "Start a loopback debug session?" in body


class TicketTransitionSwapTestCase(TestCase):
    """Acting inside the drawer must not close the drawer.

    ``ticket_transition`` redirected to ``dash:board``, so a transition triggered from
    the side panel navigated away from it — the worst of the five full-reload POSTs,
    because the panel is the only place the action exists.
    """

    def setUp(self) -> None:
        self.ticket = TicketFactory(state=State.NOT_STARTED)

    def _post(self, action: str, *, htmx: bool = True) -> object:
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        url = reverse("dash:ticket_transition", args=[self.ticket.pk])
        return self.client.post(url, {"action": action}, **headers)

    def test_an_htmx_transition_answers_the_refreshed_drawer(self) -> None:
        action = legal_transition_names(self.ticket)[0]
        response = self._post(action)
        assert response.status_code == 200
        body = response.content.decode()
        assert "<!doctype html>" not in body.lower()
        assert 'class="drawer"' in body

    def test_a_no_js_transition_keeps_the_board_redirect(self) -> None:
        action = legal_transition_names(self.ticket)[0]
        assert self._post(action, htmx=False).status_code == 302

    def test_an_illegal_transition_answers_the_drawer_with_its_reason(self) -> None:
        response = self._post("not_a_transition")
        assert response.status_code == 400
        body = response.content.decode()
        assert 'class="drawer"' in body
        assert "not_a_transition" in body

    def test_a_no_js_illegal_transition_renders_a_page_with_navigation(self) -> None:
        response = self._post("not_a_transition", htmx=False)
        assert response.status_code == 400
        body = response.content.decode()
        assert "<!doctype html>" in body.lower()
        assert reverse("dash:board") in body

    def test_the_drawer_form_is_wired_to_swap_the_drawer(self) -> None:
        body = self.client.get(reverse("dash:ticket_drawer", args=[self.ticket.pk])).content.decode()
        assert f'hx-post="{reverse("dash:ticket_transition", args=[self.ticket.pk])}"' in body
        assert 'hx-target="#drawer"' in body


class DrawerPayloadIsBoundedTestCase(TestCase):
    """A long-lived ticket's drawer must stay openable (#3873).

    The deployed board's ticket 287 answered 5,005,822 bytes — 8,959 provenance
    rows — because the drawer read every ``TicketTransition`` and every
    ``TaskAttempt`` the ticket ever accumulated. Nothing in the drawer code
    changed; the tables crossed a threshold. The existing drawer specs seed four
    tiny tickets, which is exactly why they stayed green through it, so this
    fixture is deliberately large enough to cross the caps.
    """

    #: Comfortably above what the capped drawer can emit and far below the
    #: multi-megabyte payload the uncapped one produced.
    MAX_DRAWER_BYTES = 250_000

    TRANSITIONS = 120
    TASKS = 40
    ATTEMPTS_PER_TASK = 25

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = TicketFactory(state=State.STARTED, short_description="long lived ticket")
        TicketTransition.objects.bulk_create(
            TicketTransition(
                ticket=cls.ticket,
                from_state=State.SCOPED,
                to_state=State.STARTED,
                triggered_by="start",
            )
            for _ in range(cls.TRANSITIONS)
        )
        for _ in range(cls.TASKS):
            task = TaskFactory(ticket=cls.ticket, phase="coding")
            TaskAttempt.objects.bulk_create(
                TaskAttempt(
                    task=task,
                    execution_target=Task.ExecutionTarget.HEADLESS,
                    model="claude-opus-4-8",
                    error="a recorded failure reason that occupies a realistic amount of the row",
                )
                for _ in range(cls.ATTEMPTS_PER_TASK)
            )

    def _drawer(self) -> str:
        response = self.client.get(reverse("dash:ticket_drawer", args=[self.ticket.pk]))
        assert response.status_code == 200
        self.body = response.content.decode()
        return self.body

    def test_the_drawer_still_opens_and_the_payload_stays_bounded(self) -> None:
        body = self._drawer()
        assert 'class="drawer"' in body
        assert "long lived ticket" in body
        assert len(body.encode()) < self.MAX_DRAWER_BYTES, f"drawer payload {len(body.encode())} bytes"

    def test_the_truncation_is_stated_rather_than_silent(self) -> None:
        body = self._drawer()
        assert f"of {self.TRANSITIONS}" in body, "transition history does not state its total"
        assert f"of {self.TASKS}" in body, "task list does not state its total"
        assert f"of {self.ATTEMPTS_PER_TASK}" in body, "attempt list does not state its total"


class TaskEnqueuePostTestCase(TestCase):
    """The drawer's "Review now" / "Ship now" buttons — prioritise work, never record an outcome (#4085)."""

    def setUp(self) -> None:
        self.ticket = TicketFactory(state=State.STARTED)
        self.url = reverse("dash:task_action", args=[self.ticket.pk])

    def _post(self, phase: str, *, htmx: bool = True, **extra: str) -> HttpResponse:
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(self.url, {"phase": phase, **extra}, **headers)

    def test_a_click_enqueues_an_unstarted_task_for_that_phase(self) -> None:
        response = self._post("reviewing")
        assert response.status_code == 200
        task = Task.objects.get(ticket=self.ticket, phase="reviewing")
        assert task.status == Task.Status.PENDING
        assert task.execution_reason.strip()

    def test_the_answer_is_the_refreshed_drawer_carrying_the_new_task(self) -> None:
        body = self._post("reviewing").content.decode()
        assert "<!doctype html>" not in body.lower()
        assert 'class="drawer"' in body
        task = Task.objects.get(ticket=self.ticket, phase="reviewing")
        assert f"TODO-{task.pk}" in body

    def test_the_enqueue_is_audited_with_actor_phase_and_ticket(self) -> None:
        with self.assertLogs("teatree.dash.audit", level="INFO") as logs:
            self._post("shipping")
        line = next(entry for entry in logs.output if "task:enqueue:shipping" in entry)
        assert f"target={self.ticket.pk}" in line
        assert "actor=" in line

    def test_an_explicit_reason_becomes_the_worker_prompt(self) -> None:
        self._post("reviewing", reason="PR is green and blocked on a verdict.")
        task = Task.objects.get(ticket=self.ticket, phase="reviewing")
        assert task.execution_reason == "PR is green and blocked on a verdict."

    def test_a_phase_outside_the_button_set_is_refused(self) -> None:
        response = self._post("retro")
        assert response.status_code == 400
        assert not Task.objects.filter(ticket=self.ticket).exists()

    def test_a_second_click_reports_the_queued_task_rather_than_duplicating(self) -> None:
        self._post("reviewing")
        response = self._post("reviewing")
        assert response.status_code == 400
        assert Task.objects.filter(ticket=self.ticket, phase="reviewing").count() == 1
        task = Task.objects.get(ticket=self.ticket, phase="reviewing")
        assert f"TODO-{task.pk}" in response.content.decode()

    def test_a_no_js_client_gets_a_navigable_page_on_a_refusal(self) -> None:
        response = self._post("retro", htmx=False)
        assert response.status_code == 400
        body = response.content.decode()
        assert "<!doctype html>" in body.lower()
        assert reverse("dash:board") in body

    def test_a_no_js_enqueue_keeps_the_board_redirect(self) -> None:
        assert self._post("reviewing", htmx=False).status_code == 302

    def test_a_missing_ticket_is_a_404(self) -> None:
        response = self.client.post(reverse("dash:task_action", args=[999999]), {"phase": "reviewing"})
        assert response.status_code == 404

    def test_csrf_is_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        assert csrf_client.post(self.url, {"phase": "reviewing"}).status_code == 403

    def test_a_get_is_refused(self) -> None:
        assert self.client.get(self.url).status_code == 405


class TaskEnqueueButtonsRenderTestCase(TestCase):
    def setUp(self) -> None:
        self.ticket = TicketFactory(state=State.STARTED)

    def _drawer(self) -> str:
        return self.client.get(reverse("dash:ticket_drawer", args=[self.ticket.pk])).content.decode()

    def test_the_drawer_offers_every_enqueueable_phase_wired_to_swap_the_drawer(self) -> None:
        body = self._drawer()
        assert f'hx-post="{reverse("dash:task_action", args=[self.ticket.pk])}"' in body
        for button in enqueue_buttons(self.ticket):
            assert button.label in body

    def test_a_queued_phase_renders_disabled_with_its_reason(self) -> None:
        task = TaskFactory(ticket=self.ticket, phase="reviewing", status=Task.Status.PENDING)
        body = self._drawer()
        assert "disabled" in body
        assert f"TODO-{task.pk} is already queued for reviewing" in body
