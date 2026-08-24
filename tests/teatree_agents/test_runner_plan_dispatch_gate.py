"""The dispatch chokepoint refuses an implementing agent on an unplanned ticket (#4409).

The gate's value is that it fires at the seam a dispatch cannot avoid, so this
drives ``run_agent`` end to end and asserts the harness never opened — a gate that
refuses only after the child is spawned has already paid for the run it refused.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents import harness as harness_mod
from teatree.agents import runner as runner_mod
from teatree.agents.runner import run_agent
from teatree.core.gates.plan_dispatch_gate import PLAN_MISSING_PREFIX
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream


class _DispatchProbe(TestCase):
    """Drives ``run_agent`` with the SDK child replaced by a spy that records spawns."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        self.spawned: list[object] = []

    def _dispatch(self, phase: str) -> Task:
        def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
            self.spawned.append(options)
            return FakeHarnessSession(success_stream({"summary": "ok"}))

        session = Session.objects.create(ticket=self.ticket, agent_id=phase)
        task = Task.objects.create(ticket=self.ticket, session=session, phase=phase)
        snapshot = runner_mod.TaskUsage(turns=0, cost_usd=0.0)
        with (
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _make_client),
            # The watchdog samples usage from a worker thread, whose own connection
            # sees a different in-memory SQLite — pinned, as in every sibling drive.
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
        ):
            run_agent(task, phase=phase, overlay_skill_metadata={})
        task.refresh_from_db()
        return task


class TestUnplannedImplementingDispatchIsRefused(_DispatchProbe):
    def test_coding_dispatch_is_refused_before_the_harness_opens(self) -> None:
        task = self._dispatch("coding")

        assert self.spawned == [], "no agent child may be spawned for an unplanned implementing dispatch"
        assert task.status == Task.Status.FAILED
        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert attempt.error.startswith(PLAN_MISSING_PREFIX)
        assert "skip-planning" in attempt.error

    def test_a_testing_dispatch_is_refused_and_bills_no_spend(self) -> None:
        task = self._dispatch("testing")

        assert self.spawned == []
        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert attempt.error.startswith(PLAN_MISSING_PREFIX)
        # Refused before the harness opened, so no turn was billed — NULL, not zero.
        assert attempt.cost_usd is None


class TestARecordedDecisionLetsTheDispatchThrough(_DispatchProbe):
    def test_a_plan_artifact_lets_the_coder_spawn(self) -> None:
        PlanArtifact.record(ticket=self.ticket, plan_text="Do X by Y", recorded_by="t3:planner")

        self._dispatch("coding")

        assert self.spawned, "a planned ticket must still dispatch its coder"

    def test_a_trivial_skip_marker_lets_the_coder_spawn(self) -> None:
        mark_trivial_plan_skip(self.ticket, reason="one-line constant bump")

        self._dispatch("coding")

        assert self.spawned, "a recorded trivial skip must still dispatch its coder"


class TestNonImplementingDispatchIsUntouched(_DispatchProbe):
    def test_a_planner_spawns_on_an_unplanned_ticket(self) -> None:
        # The planner is what RECORDS the plan — gating it would deadlock the ticket.
        self._dispatch("planning")

        assert self.spawned, "the planning dispatch must never be gated on a plan"
