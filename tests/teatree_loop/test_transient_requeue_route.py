"""How the transient sweep routes a FAILED task: a crash is reopened, a cancel is parked, a fault is escalated."""

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

import teatree.agents.runner as runner_mod
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.modelkit.task_failure_taxonomy import SUPERSEDED_PREFIX
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.transient_requeue import HALT_STAMP, escalation_marker, requeue_transient_failed
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import (
    CLAUDE_LIKE,
    MANAGED,
    CrashingSession,
    register_stub_harnesses,
    route_config,
    routed_by,
)


class TestRoutedCrashIsRequeuedOntoTheNextCandidate(TestCase):
    def test_the_sweep_reopens_the_task_and_the_next_dispatch_resolves_candidate_one(self) -> None:
        register_stub_harnesses(self, MANAGED, CLAUDE_LIKE, sessions={MANAGED: CrashingSession})
        ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="debugging")
        config = route_config("route-skill", MANAGED, CLAUDE_LIKE)

        with (
            routed_by(config),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=["route-skill"]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            run_agent(task, phase="debugging", overlay_skill_metadata=SkillMetadata())
            reopened = requeue_transient_failed()
            task.refresh_from_db()
            next_dispatch = resolve_dispatch_harness(task, phase="debugging", skills=["route-skill"])

        assert reopened == 1
        assert task.status == Task.Status.PENDING
        assert (next_dispatch.name, next_dispatch.route_candidate_index) == (CLAUDE_LIKE, 1)


class TestACancelIsParkedWhileAFaultStillEscalates(TestCase):
    _CANCEL_REASON = "the fix landed by hand"

    def setUp(self) -> None:
        super().setUp()
        self.ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        self.cancelled = self._task("coding")
        self.ticket.merge_extra(
            merge_into_dicts={"pydantic_ai_threads": {str(self.cancelled.pk): [{"kind": "request"}]}}
        )
        call_command("tasks", "cancel", self.cancelled.pk, reason=self._CANCEL_REASON)
        self.superseded = self._task("testing")
        self.superseded.fail(
            reason=f"{SUPERSEDED_PREFIX}ticket reworked — this task's phase is being redone", by_holder=False
        )
        self.broken = self._task("reviewing")
        self.broken.fail(reason="AssertionError: widget count was 3, expected 4", by_holder=False)
        TaskAttempt.objects.create(
            task=self.broken,
            ended_at=timezone.now(),
            exit_code=1,
            error="AssertionError: widget count was 3, expected 4",
        )

        self.reopened = requeue_transient_failed()

    def _task(self, phase: str) -> Task:
        session = Session.objects.create(ticket=self.ticket, agent_id=phase)
        return Task.objects.create(ticket=self.ticket, session=session, phase=phase)

    def _halt_row(self, task: Task) -> DeferredQuestion:
        return DeferredQuestion.objects.get(dedupe_marker=escalation_marker(task))

    def test_cancelled_task_parks_without_question(self) -> None:
        self.cancelled.refresh_from_db()
        self.ticket.refresh_from_db()

        assert self.reopened == 0
        assert not DeferredQuestion.objects.filter(question__contains=self._CANCEL_REASON).exists()
        assert DeferredQuestion.pending().count() == 2
        assert self.cancelled.status == Task.Status.FAILED
        assert HALT_STAMP in self.cancelled.execution_reason
        assert str(self.cancelled.pk) not in self.ticket.extra.get("pydantic_ai_threads", {})

    def test_superseded_without_successor_still_escalates_internal(self) -> None:
        assert self._halt_row(self.superseded).audience == DeferredQuestion.Audience.INTERNAL

    def test_unclassified_failure_still_escalates_internal_halt(self) -> None:
        row = self._halt_row(self.broken)

        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.dedupe_marker.startswith("repair-halt:")
