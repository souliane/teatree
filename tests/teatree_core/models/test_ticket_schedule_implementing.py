"""No implementing Task is minted on a ticket nobody planned; the router plans it instead.

The dispatch gate refused a plan-less coder AFTER the Task existed, so every synthetic
producer minted a Task that could only fail ``plan_missing`` and wait for a drain sweep
to reroute it. The three fresh mint seams now refuse at the write, and
``Ticket.schedule_implementing`` routes an unplanned early ticket to planning.
"""

import pytest
from django.test import TestCase

from teatree.core.modelkit.phases import IMPLEMENTING_PHASES, subagent_for_phase
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.plan_decision import PLAN_MISSING_PREFIX
from teatree.core.models.task_enqueue import enqueue_phase_task, enqueue_phase_task_once
from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip
from teatree.loop.persistence_phase_task import create_phase_task
from tests.factories import planned_ticket, record_test_plan


def _unplanned(state: str = Ticket.State.NOT_STARTED) -> Ticket:
    return Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=state)


class TestFreshMintSeamsRefuseAnUnplannedImplementingPhase(TestCase):
    def test_the_fsm_scheduler_refuses_and_writes_nothing(self) -> None:
        for phase in sorted(IMPLEMENTING_PHASES):
            with self.subTest(phase=phase):
                ticket = _unplanned()

                with pytest.raises(NoPlanArtifactError, match=PLAN_MISSING_PREFIX):
                    ticket._schedule_phase_task(phase, "reason", None)

                assert not Task.objects.filter(ticket=ticket).exists()
                assert not Session.objects.filter(ticket=ticket).exists()

    def test_the_loop_mint_refuses_and_writes_nothing(self) -> None:
        for phase in sorted(IMPLEMENTING_PHASES):
            with self.subTest(phase=phase):
                ticket = _unplanned()

                with pytest.raises(NoPlanArtifactError, match=PLAN_MISSING_PREFIX):
                    create_phase_task(ticket, phase=phase, agent_id="red-card", reason="fix it")

                assert not Task.objects.filter(ticket=ticket).exists()
                assert not Session.objects.filter(ticket=ticket).exists()

    def test_the_operator_enqueue_refuses_both_shapes(self) -> None:
        ticket = _unplanned()

        with pytest.raises(NoPlanArtifactError, match=PLAN_MISSING_PREFIX):
            enqueue_phase_task(ticket=ticket, phase="code", reason="implement it")
        with pytest.raises(NoPlanArtifactError, match=PLAN_MISSING_PREFIX):
            enqueue_phase_task_once(ticket=ticket, phase="coding", reason="implement it")

        assert not Task.objects.filter(ticket=ticket).exists()

    def test_schedule_coding_on_a_bare_ticket_is_refused(self) -> None:
        with pytest.raises(NoPlanArtifactError):
            _unplanned().schedule_coding()


class TestFreshMintSeamsStillMintWhatTheyShould(TestCase):
    def test_a_planned_ticket_mints_its_coding_task(self) -> None:
        ticket = planned_ticket(overlay="test")

        assert create_phase_task(ticket, phase="coding", agent_id="red-card", reason="fix it").phase == "coding"

    def test_a_skip_marked_ticket_mints_without_a_plan(self) -> None:
        ticket = _unplanned()
        mark_trivial_plan_skip(ticket, reason="one-line constant bump")

        assert ticket.schedule_coding().phase == "coding"

    def test_non_implementing_phases_mint_on_an_unplanned_ticket(self) -> None:
        ticket = _unplanned()

        assert ticket.schedule_planning().phase == "planning"
        assert ticket.schedule_review().phase == "reviewing"
        assert create_phase_task(ticket, phase="answering", agent_id="answerer", reason="answer").phase == "answering"
        assert enqueue_phase_task(ticket=ticket, phase="scoping", reason="Decide X.").phase == "scoping"


class TestScheduleImplementingRoutes(TestCase):
    def test_a_planned_ticket_gets_the_phase_with_the_callers_reason(self) -> None:
        ticket = planned_ticket(overlay="test", state=Ticket.State.PLAN_RECORDED)

        task = ticket.schedule_implementing("debugging", reason="Auto-scheduled red-MR fix")

        assert (task.phase, task.execution_reason) == ("debugging", "Auto-scheduled red-MR fix")

    def test_an_unplanned_early_ticket_is_routed_to_planning_carrying_the_intent(self) -> None:
        for state in (Ticket.State.NOT_STARTED, Ticket.State.SCOPED, Ticket.State.WORK_STARTED):
            with self.subTest(state=state):
                ticket = _unplanned(state)

                task = ticket.schedule_implementing("coding", reason="Auto-scheduled RED CARD corrective action")

                ticket.refresh_from_db()
                assert task.phase == "planning"
                assert ticket.state == Ticket.State.WORK_STARTED
                assert "Auto-scheduled RED CARD corrective action" in task.execution_reason
                assert not Task.objects.filter(ticket=ticket, phase="coding").exists()

    def test_an_unplanned_ticket_past_the_early_states_is_refused(self) -> None:
        ticket = _unplanned(Ticket.State.CODED)

        with pytest.raises(NoPlanArtifactError, match=PLAN_MISSING_PREFIX):
            ticket.schedule_implementing("testing", reason="test it")

        assert not Task.objects.filter(ticket=ticket).exists()

    def test_a_repeated_route_reuses_the_in_flight_planning_task(self) -> None:
        ticket = _unplanned()

        first = ticket.schedule_implementing("coding", reason="first")
        second = ticket.schedule_implementing("coding", reason="second")

        assert second.pk == first.pk


class TestBeginPlanningCarriesTheIntent(TestCase):
    def test_the_intent_rides_the_planning_reason(self) -> None:
        task = _unplanned().begin_planning(intent="fold dream gaps 3a9f, 77c1")

        assert task.execution_reason.startswith("Auto-scheduled planning")
        assert "fold dream gaps 3a9f, 77c1" in task.execution_reason

    def test_no_intent_leaves_the_reason_unchanged(self) -> None:
        task = _unplanned().begin_planning()

        assert task.execution_reason == "Auto-scheduled planning — produce a plan before coding"


class TestPlanningHandsOffTheRequestedPhase(TestCase):
    """An unplanned debugging/e2e request keeps its phase and reason through planning."""

    def _planned_next(self, phase: str, reason: str) -> Task:
        ticket = _unplanned()
        planning = ticket.schedule_implementing(phase, reason=reason)
        record_test_plan(ticket)
        planning.refresh_from_db()
        planning.complete()
        return Task.objects.filter(ticket=ticket).exclude(phase="planning").get()

    def test_a_debugging_request_is_handed_to_the_debugger_after_planning(self) -> None:
        task = self._planned_next("debugging", "Auto-scheduled red-MR fix — debug https://x/pr/5")

        assert (task.phase, task.execution_reason) == ("debugging", "Auto-scheduled red-MR fix — debug https://x/pr/5")
        assert subagent_for_phase("author", task.phase) == "t3:debugger"

    def test_an_e2e_request_is_handed_to_the_e2e_agent_after_planning(self) -> None:
        task = self._planned_next("e2e", "Auto-scheduled E2E fix — e2e/specs/login.spec.ts")

        assert task.phase == "e2e"
        assert subagent_for_phase("author", task.phase) == "t3:e2e"

    def test_a_coding_request_keeps_its_own_reason(self) -> None:
        task = self._planned_next("code", "Auto-scheduled skill-drift fix — code/SKILL.md")

        assert (task.phase, task.execution_reason) == ("coding", "Auto-scheduled skill-drift fix — code/SKILL.md")

    def test_a_ticket_planned_without_a_routed_request_gets_generic_coding(self) -> None:
        ticket = _unplanned()
        planning = ticket.begin_planning()
        record_test_plan(ticket)
        planning.refresh_from_db()
        planning.complete()

        task = Task.objects.filter(ticket=ticket).exclude(phase="planning").get()
        assert (task.phase, task.execution_reason) == ("coding", "Auto-scheduled coding — implement the ticket")

    def test_a_stuck_planned_ticket_is_redispatched_to_its_handed_off_phase(self) -> None:
        ticket = _unplanned()
        ticket.schedule_implementing("debugging", reason="Auto-scheduled red-MR fix — debug https://x/pr/5")
        record_test_plan(ticket)
        Task.objects.filter(ticket=ticket).update(status=Task.Status.COMPLETED)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.PLAN_RECORDED)
        ticket.refresh_from_db()

        assert ticket.schedule_planned_work().phase == "debugging"


class TestTheHandoffIsSpentOnce(TestCase):
    def _handed_off_to_debugging(self) -> Ticket:
        ticket = _unplanned()
        planning = ticket.schedule_implementing("debugging", reason="Auto-scheduled red-MR fix — debug https://x/pr/5")
        record_test_plan(ticket)
        planning.refresh_from_db()
        planning.complete()
        ticket.refresh_from_db()
        return ticket

    def _replan(self, ticket: Ticket) -> Task:
        Task.objects.filter(ticket=ticket).update(status=Task.Status.COMPLETED)
        planning = ticket.schedule_planning()
        record_test_plan(ticket)
        planning.refresh_from_db()
        planning.complete()
        return Task.objects.filter(ticket=ticket, status=Task.Status.PENDING).exclude(phase="planning").get()

    def test_applying_the_handoff_consumes_it(self) -> None:
        ticket = self._handed_off_to_debugging()

        assert ticket.state == Ticket.State.PLAN_RECORDED
        assert "planning_handoff" not in ticket.extra

    def test_a_reworked_ticket_re_planned_hands_off_to_coding(self) -> None:
        ticket = self._handed_off_to_debugging()
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.CODED)
        ticket.refresh_from_db()
        ticket.rework()
        ticket.save()

        task = self._replan(ticket)

        assert (task.phase, task.execution_reason) == ("coding", "Auto-scheduled coding — implement the ticket")

    def test_rework_and_reopen_drop_a_handoff_that_was_never_applied(self) -> None:
        stale = {"planning_task": 1, "phase": "debugging", "reason": "old red-MR fix"}
        for source, back in ((Ticket.State.CODED, Ticket.rework), (Ticket.State.MERGED, Ticket.reopen)):
            with self.subTest(source=source):
                ticket = Ticket.objects.create(
                    overlay="test", role=Ticket.Role.AUTHOR, state=source, extra={"planning_handoff": stale}
                )

                back(ticket)
                ticket.save()

                ticket.refresh_from_db()
                assert "planning_handoff" not in ticket.extra
