"""Per-phase auto-dispatch tests (souliane/teatree#443 split of test_models.py)."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from tests.teatree_core.models._shared import (
    _advance_started_to_planned,
    _advance_ticket_to_tested,
    _attach_shippable_worktree,
    _complete_phase_task,
    _start_with_provision,
)


class TestPhaseAutoDispatch(TestCase):
    """Auto-dispatch of next-phase tasks at each phase boundary (issue #364)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_start_provisions_then_schedules_planning_task(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()

        _start_with_provision(self, ticket)

        ticket.refresh_from_db()
        task = ticket.tasks.get(phase="planning")
        # Planning is loop-dispatched ((author, planning) → t3:planner), so it
        # runs as an in-session sub-agent (subscription-covered), not a metered
        # detached claude -p.
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert task.session.agent_id == "planning"
        assert ticket.state == Ticket.State.STARTED

    def test_code_auto_schedules_testing_task(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        _advance_started_to_planned(ticket)
        ticket.code()
        ticket.save()

        task = ticket.tasks.get(phase="testing")
        # Testing is loop-dispatched ((author, testing) → t3:tester) → in-session.
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert task.session.agent_id == "testing"
        assert ticket.state == Ticket.State.CODED

    def test_scoping_task_completion_advances_to_started(self) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        def fake_enqueue(ticket_id: int) -> None:
            target = Ticket.objects.get(pk=ticket_id)
            if target.state == Ticket.State.STARTED:
                target.schedule_planning()

        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        session = Session.objects.create(ticket=ticket, agent_id="scoper")
        task = Task.objects.create(ticket=ticket, session=session, phase="scoping")

        fake_task = MagicMock()
        fake_task.enqueue.side_effect = fake_enqueue
        task.claim(claimed_by="worker")
        with (
            patch.object(tasks_mod, "execute_provision", fake_task),
            self.captureOnCommitCallbacks(execute=True),
        ):
            task.complete()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        # execute_provision (worker side effect of start()) scheduled the planning task
        assert ticket.tasks.filter(phase="planning", status=Task.Status.PENDING).exists()

    def test_coding_task_completion_advances_to_coded(self) -> None:
        from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        _start_with_provision(self, ticket)

        PlanArtifact.record(ticket=ticket, plan_text="Plan: implement", recorded_by="t3:planner")
        _complete_phase_task(ticket, "planning")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED

        _complete_phase_task(ticket, "coding")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        # code() auto-scheduled a testing task
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).exists()

    def test_testing_task_completion_advances_to_tested(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        _start_with_provision(self, ticket)
        _advance_started_to_planned(ticket)
        ticket.code()
        ticket.save()

        _complete_phase_task(ticket, "testing")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        # test() auto-scheduled a reviewing task
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).exists()

    def test_shipping_defaults_to_interactive_without_t3_auto_ship(self) -> None:
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {"T3_MODE": "interactive"}, clear=False) as env:
            env.pop("T3_AUTO_SHIP", None)
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert "user approval" in task.execution_reason

    def test_shipping_is_auto_when_db_mode_is_auto(self) -> None:
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("mode", value="auto")
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("T3_AUTO_SHIP", None)
            env.pop("T3_MODE", None)
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert "auto mode" in task.execution_reason

    def test_shipping_gates_on_user_approval_when_db_mode_is_manual(self) -> None:
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("mode", value="interactive")
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("T3_AUTO_SHIP", None)
            env.pop("T3_MODE", None)
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert "user approval" in task.execution_reason

    def test_t3_auto_ship_env_no_longer_overrides_db_mode_manual(self) -> None:
        # #2697 behaviour change: the T3_AUTO_SHIP env short-circuit is gone.
        # An env T3_AUTO_SHIP=true can no longer force auto-ship when the DB
        # resolves mode=manual — the resolved mode is the sole authority.
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("mode", value="interactive")
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {"T3_AUTO_SHIP": "true"}, clear=False) as env:
            env.pop("T3_MODE", None)
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert "user approval" in task.execution_reason

    def test_shipping_is_interactive_with_auto_reason_when_global_mode_is_auto(self) -> None:
        # teatree.mode = auto (or T3_MODE=auto) is blanket publish consent, but
        # the ship still runs in-session — only the reason reflects auto mode.
        ticket = Ticket.objects.create()

        with patch.dict("os.environ", {"T3_MODE": "auto"}, clear=False) as env:
            env.pop("T3_AUTO_SHIP", None)
            task = ticket.schedule_shipping()

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert "auto mode" in task.execution_reason

    def test_shipping_task_completion_advances_to_shipped(self) -> None:
        # #1284 (codex #1282-2): the task-based shipping path now enforces
        # ``Session.check_gate_across_ticket("shipping")`` so a happy-path
        # advance must have ``testing`` and ``reviewing`` recorded. Pre-fix
        # this test passed even without ``testing`` recorded — exactly the
        # gate-bypass codex flagged. The fix here is to record the testing
        # phase visit through the same task path the loop uses, mirroring
        # the real lifecycle.
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        # _advance_ticket_to_tested fires the FSM directly (raw test()) so the
        # testing phase visit is not recorded by the helper. Record it now,
        # symmetrically with how the loop would have done it when the testing
        # task completed.
        testing_session = Session.objects.create(ticket=ticket, agent_id="testing")
        testing_session.visit_phase("testing", agent_id="testing")
        _complete_phase_task(ticket, "reviewing")
        # reviewing completion → REVIEWED + shipping task (interactive by default)

        _complete_phase_task(ticket, "shipping")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_direct_ship_consumes_pending_shipping_task(self) -> None:
        """Regression #471: direct ship() must consume the pending shipping task.

        When ``pr.py`` calls ``ticket.ship()`` directly the auto-scheduled
        PENDING shipping task would otherwise be claimed by the dispatcher
        as a zombie session after the work is already done.
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        # review() scheduled an interactive shipping task that is still PENDING
        shipping_task = ticket.tasks.get(phase="shipping")
        assert shipping_task.status == Task.Status.PENDING

        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_ship", MagicMock()),
        ):
            ticket.ship()
            ticket.save()

        shipping_task.refresh_from_db()
        assert shipping_task.status == Task.Status.COMPLETED

    def test_direct_ship_consumes_claimed_shipping_task(self) -> None:
        """Orphan task already CLAIMED by a dispatcher race must still be consumed.

        The dispatcher may have polled and claimed the shipping task between
        the user invoking the manual ship and the FSM transition firing.
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        shipping_task = ticket.tasks.get(phase="shipping")
        shipping_task.claim(claimed_by="zombie-dispatcher")

        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_ship", MagicMock()),
        ):
            ticket.ship()
            ticket.save()

        shipping_task.refresh_from_db()
        assert shipping_task.status == Task.Status.COMPLETED

    def test_direct_review_consumes_pending_reviewing_task(self) -> None:
        """Same regression for ``ticket.review()`` and the reviewing task."""
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)
        # test() scheduled a PENDING reviewing task. We satisfy the FSM guard
        # by also marking that task COMPLETED before review() — but the bug
        # would surface if a second reviewing task were left pending.
        first_review = ticket.tasks.get(phase="reviewing")
        first_review.claim(claimed_by="t")
        first_review.complete()
        # A second reviewing task could exist (e.g. retry); simulate one.
        ticket.refresh_from_db()
        ticket.state = Ticket.State.TESTED
        ticket.save(update_fields=["state"])
        zombie = ticket.schedule_review()
        assert zombie.status == Task.Status.PENDING

        ticket.review()
        ticket.save()

        zombie.refresh_from_db()
        assert zombie.status == Task.Status.COMPLETED

    def test_task_driven_path_unaffected(self) -> None:
        """Task-completion path stays a single-shipping-task chain.

        ``Task.complete()`` marks the task COMPLETED before
        ``_advance_ticket()`` fires the transition, so the consume call is a
        no-op (zero-row UPDATE). Verify no duplicate task is created.
        """
        ticket = Ticket.objects.create()
        _attach_shippable_worktree(ticket, self._tmp_path)
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED
        shipping_tasks = list(ticket.tasks.filter(phase="shipping"))
        assert len(shipping_tasks) == 1
        assert shipping_tasks[0].status == Task.Status.PENDING

    def test_start_enqueues_execute_provision_after_commit(self) -> None:
        """Stage 3 of #140: start() body offloads provisioning to a @task worker."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()

        fake_task = MagicMock()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_provision", fake_task),
        ):
            ticket.start()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        fake_task.enqueue.assert_called_once_with(ticket.pk)

    def test_mark_merged_enqueues_execute_teardown_after_commit(self) -> None:
        """Stage 5 of #140: mark_merged() body offloads teardown to a @task worker."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        ticket.state = Ticket.State.IN_REVIEW
        ticket.save(update_fields=["state"])

        fake_task = MagicMock()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_teardown", fake_task),
        ):
            ticket.mark_merged()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        fake_task.enqueue.assert_called_once_with(ticket.pk)

    def test_ship_enqueues_execute_ship_after_commit(self) -> None:
        """Stage 2 of #140: ship() body offloads I/O to a @task worker."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core import tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)
        _complete_phase_task(ticket, "reviewing")

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

        fake_task = MagicMock()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(tasks_mod, "execute_ship", fake_task),
        ):
            ticket.ship()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        fake_task.enqueue.assert_called_once_with(ticket.pk)

    def test_child_task_of_already_advanced_ticket_is_noop(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        session = Session.objects.create(ticket=ticket, agent_id="scoper")
        first = Task.objects.create(ticket=ticket, session=session, phase="scoping")
        second = Task.objects.create(ticket=ticket, session=session, phase="scoping")

        first.claim(claimed_by="worker-1")
        first.complete()
        # First completion advanced SCOPED → STARTED
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

        second.claim(claimed_by="worker-2")
        second.complete()
        # Second completion no-ops because state is no longer SCOPED
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
