"""A routed crash is reopened by the transient sweep and the task's next dispatch resolves the next candidate."""

from unittest.mock import patch

from django.test import TestCase

import teatree.agents.runner as runner_mod
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task, Ticket
from teatree.loop.transient_requeue import requeue_transient_failed
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
