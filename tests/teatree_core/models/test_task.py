"""Task model tests (souliane/teatree#443 split of test_models.py).

Lifecycle, child-task spawning, and ``build_task_detail``.
"""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase
from django.utils import timezone

import teatree.utils.singleton as singleton_mod
from teatree.core.models import DeferredQuestion, InvalidTransitionError, Session, Task, TaskAttempt, Ticket, Worktree
from teatree.core.models.task_attempt import TaskAttemptQuerySet
from teatree.core.worktree.occupancy import WorktreeOccupiedError, acquire, occupancy_holder, task_holder_id

_FAKE_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestTask(TestCase):
    def test_claim_route_complete_fail_and_attempt_storage(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)

        task.claim(claimed_by="worker-1", lease_seconds=120)
        first_expiry = task.lease_expires_at
        assert first_expiry is not None

        task.renew_lease(lease_seconds=300)
        task.complete(result_artifact_path="/tmp/result.json")

        failed_task = Task.objects.create(ticket=ticket, session=session)
        failed_task.fail(reason="test: deliberate failure", by_holder=True)

        attempt = TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=0,
            artifact_path="/tmp/result.json",
        )

        task.refresh_from_db()
        failed_task.refresh_from_db()

        assert task.status == Task.Status.COMPLETED
        assert task.result_artifact_path == "/tmp/result.json"
        assert task.claimed_by == ""
        assert task.lease_expires_at is None
        assert failed_task.status == Task.Status.FAILED
        assert str(task) == f"task-{task.pk}"
        assert str(attempt) == f"attempt-{attempt.pk}"

    def test_claim_rejects_an_active_lease(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        task.claim(claimed_by="worker-1")

        with pytest.raises(InvalidTransitionError, match="Task already claimed"):
            task.claim(claimed_by="worker-2")

    def test_claim_rejects_terminal_tasks(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        completed = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        failed = Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)

        with pytest.raises(InvalidTransitionError, match="Task already finished"):
            completed.claim(claimed_by="worker-1")

        with pytest.raises(InvalidTransitionError, match="Task already finished"):
            failed.claim(claimed_by="worker-2")

    def test_complete_with_attempt_records_success_and_failure(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")

        success_task = Task.objects.create(ticket=ticket, session=session)
        attempt = success_task.complete_with_attempt(artifact_path="/tmp/ok.json")
        success_task.refresh_from_db()
        assert success_task.status == Task.Status.COMPLETED
        assert attempt.exit_code == 0
        assert attempt.artifact_path == "/tmp/ok.json"

        failure_task = Task.objects.create(ticket=ticket, session=session)
        attempt = failure_task.complete_with_attempt(exit_code=1, error="boom")
        failure_task.refresh_from_db()
        assert failure_task.status == Task.Status.FAILED
        assert attempt.exit_code == 1
        assert attempt.error == "boom"

    def test_reopen_failed_task_resets_to_pending(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)

        task.reopen()
        task.refresh_from_db()

        assert task.status == Task.Status.PENDING

    def test_reopen_non_failed_task_raises(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)

        with pytest.raises(InvalidTransitionError, match="Can only reopen failed tasks"):
            task.reopen()


class TestClaimedBySessionPersistence(TestCase):
    """``claimed_by_session`` is written by ``claim`` and blanked on every return/terminal path (F1.5).

    Pre-fix ``claim`` never SET the column and the ``complete``/``fail``/``park``/
    ``_route`` ``update_fields`` lists omitted it, so ``_clear_claim`` blanked it
    only in memory — the DB kept a stale attribution and the ``renew_lease``
    claim-generation CAS (which filters on ``claimed_by_session``) operated on it.
    """

    def _task(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        return Task.objects.create(ticket=ticket, session=session)

    def _db_session(self, task: Task) -> str:
        # Read the persisted column directly — never the in-memory instance.
        return Task.objects.values_list("claimed_by_session", flat=True).get(pk=task.pk)

    def test_claim_persists_session_to_db(self) -> None:
        task = self._task()
        task.claim(claimed_by="worker", claimed_by_session="sess-1")
        assert self._db_session(task) == "sess-1"
        # claim() ends with refresh_from_db, so the instance agrees with the row.
        assert task.claimed_by_session == "sess-1"

    def test_claim_defaults_session_to_empty(self) -> None:
        task = self._task()
        task.claim(claimed_by="worker")
        assert self._db_session(task) == ""

    def test_complete_blanks_session_in_db(self) -> None:
        task = self._task()
        task.claim(claimed_by="worker", claimed_by_session="sess-1")
        task.complete()
        assert self._db_session(task) == ""

    def test_complete_surfacing_advance_failure_blanks_session_in_db(self) -> None:
        task = self._task()
        task.claim(claimed_by="worker", claimed_by_session="sess-1")
        task.complete_surfacing_advance_failure()
        assert self._db_session(task) == ""

    def test_fail_blanks_session_in_db(self) -> None:
        task = self._task()
        task.claim(claimed_by="worker", claimed_by_session="sess-1")
        task.fail(reason="test: deliberate failure", by_holder=True)
        assert self._db_session(task) == ""

    def test_park_blanks_session_in_db(self) -> None:
        task = self._task()
        task.claim(claimed_by="worker", claimed_by_session="sess-1")
        task.park(not_before=timezone.now() + timezone.timedelta(minutes=5))
        assert self._db_session(task) == ""

    def test_reclaim_overwrites_stale_session_in_db(self) -> None:
        # A CLAIMED task whose lease has expired is reclaimable; a fresh claim
        # must overwrite the dead owner's session so the row's attribution is
        # truthful (renew_lease's CAS filters on it).
        task = self._task()
        task.claim(claimed_by="worker-A", claimed_by_session="sess-A")
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timezone.timedelta(minutes=5))
        reclaimer = Task.objects.get(pk=task.pk)
        reclaimer.claim(claimed_by="worker-B", claimed_by_session="sess-B")
        assert self._db_session(task) == "sess-B"


class TestNeedsUserInputHeadlessLane(TestCase):
    """A headless ``needs_user_input`` parks a correlated DeferredQuestion, not an interactive task.

    In the SDK/headless lane there is no human terminal to claim an
    interactive followup, so the durable record is a mirror-pending
    ``DeferredQuestion`` correlated to the parked task (its ``parked_task``
    FK). The tick-level poster scanner posts it to Slack; the reply re-queues
    a headless resume. The interactive lane keeps its in-session followup.
    """

    def _parked_headless_task(self, *, reason: str = "Which DB host?") -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id=_FAKE_UUID)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
        )
        TaskAttempt.objects.create(
            task=task,
            agent_session_id=_FAKE_UUID,
            result={"needs_user_input": True, "user_input_reason": reason},
        )
        return task

    def test_headless_runtime_records_correlated_deferred_question(self) -> None:
        task = self._parked_headless_task()

        task.complete()

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        question = DeferredQuestion.objects.get()
        assert question.parked_task_id == task.pk
        assert "Which DB host?" in question.question
        assert question.slack_ts == ""
        assert question.is_pending


class TestTaskDisplaySubject(TestCase):
    """``display_subject()`` yields a human-readable title, never the bare phase token."""

    def test_prefers_explicit_subject(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/7")
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", subject="Hand-written summary")

        assert task.display_subject() == "Hand-written summary"

    def test_derives_from_ticket_short_description(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/issues/42",
            short_description="Improve the export pipeline",
        )
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        assert task.display_subject() == "#42 Improve the export pipeline"

    def test_derives_from_extra_issue_title_when_no_short_description(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/issues/99",
            extra={"issue_title": "Export pipeline rework"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        assert task.display_subject() == "#99 Export pipeline rework"

    def test_falls_back_to_phase_when_no_title(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="dogfood-smoke://acme")
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="dogfood_smoke")

        assert task.display_subject() == f"#{ticket.pk} dogfood_smoke"

    def test_never_returns_the_bare_phase_token(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/3")
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="short_describe")

        subject = task.display_subject()
        assert subject.strip()
        assert subject != "short_describe"


class TestTaskAttemptQuerySet(TestCase):
    """The relocated ``TaskAttempt`` manager still yields a ``TaskAttemptQuerySet``."""

    def test_manager_returns_task_attempt_queryset(self) -> None:
        assert isinstance(TaskAttempt.objects.all(), TaskAttemptQuerySet)


class TestChildTaskSpawning(TestCase):
    def test_spawn_child_tasks_creates_per_repo_tasks(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="worker")
        parent = Task.objects.create(ticket=ticket, session=session, phase="coding")

        children = parent.spawn_child_tasks(["backend", "frontend", "translations"])

        assert len(children) == 3
        assert all(c.parent_task_id == parent.pk for c in children)
        assert all(c.phase == "coding" for c in children)
        assert [c.execution_reason for c in children] == [
            "Repo: backend",
            "Repo: frontend",
            "Repo: translations",
        ]

    def test_all_children_done(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        parent = Task.objects.create(ticket=ticket, session=session)
        children = parent.spawn_child_tasks(["a", "b"])

        assert not parent.all_children_done()

        children[0].status = Task.Status.COMPLETED
        children[0].save(update_fields=["status"])
        assert not parent.all_children_done()

        children[1].status = Task.Status.FAILED
        children[1].save(update_fields=["status"])
        assert parent.all_children_done()


class TestBuildTaskDetail(TestCase):
    def test_returns_full_lineage(self) -> None:
        from teatree.core.selectors import build_task_detail  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="test")
        parent = Task.objects.create(ticket=ticket, session=session, phase="coding")
        child = Task.objects.create(ticket=ticket, session=session, phase="reviewing", parent_task=parent)
        TaskAttempt.objects.create(
            task=parent,
            exit_code=0,
            result={"summary": "done", "files_modified": ["/a.py"]},
        )

        detail = build_task_detail(parent.pk)
        assert detail is not None
        assert detail.task_id == parent.pk
        assert detail.parent is None
        assert len(detail.children) == 1
        assert detail.children[0].task_id == child.pk
        assert len(detail.attempts) == 1
        assert detail.attempts[0].result == {"summary": "done", "files_modified": ["/a.py"]}

        child_detail = build_task_detail(child.pk)
        assert child_detail is not None
        assert child_detail.parent is not None
        assert child_detail.parent.task_id == parent.pk
        assert child_detail.children == []

    def test_returns_none_for_missing(self) -> None:
        from teatree.core.selectors import build_task_detail  # noqa: PLC0415

        assert build_task_detail(999999) is None


class TaskOccupancyReleaseTests(TestCase):
    """``complete()``/``fail()`` release their own occupancy claim (souliane/teatree#4867).

    Before #4867 the ONLY release path was ``occupy_ticket_checkout``'s ``finally``
    in ``run_agent`` — an in-process context-manager unwind a process kill between
    the status write and that unwind skips entirely, stranding the claim for the
    rest of its TTL and refusing the ticket's own next-phase task.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        checkout = tempfile.mkdtemp()
        self.addCleanup(lambda: Path(checkout).exists() and Path(checkout).rmdir())
        self.worktree = Worktree.objects.create(
            ticket=self.ticket,
            overlay="t3-teatree",
            repo_path="souliane/teatree",
            branch="feat/4867",
            state=Worktree.State.PROVISIONED,
            extra={"worktree_path": checkout},
        )

    def fresh_worktree(self) -> Worktree:
        return Worktree.objects.get(pk=self.worktree.pk)

    def test_complete_releases_the_occupancy_claim_it_holds(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        task.claim(claimed_by="worker-1", claimed_by_session="sess-1")
        acquire(self.worktree, holder=task_holder_id(task), holder_session=task.claimed_by_session)

        task.complete(result_artifact_path="/tmp/result.json")

        assert occupancy_holder(self.fresh_worktree()) is None

    def test_fail_releases_the_occupancy_claim_it_holds(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        task.claim(claimed_by="worker-1", claimed_by_session="sess-1")
        acquire(self.worktree, holder=task_holder_id(task), holder_session=task.claimed_by_session)

        task.fail(reason="test: deliberate failure", by_holder=True)

        assert occupancy_holder(self.fresh_worktree()) is None

    def test_third_party_fail_of_a_live_claim_keeps_the_occupancy_claim(self) -> None:
        """#4872: an operator cancel / ``ticket.rework()`` of a live holder must not evict it."""
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        task.claim(claimed_by="worker-1", claimed_by_session="sess-1")
        acquire(self.worktree, holder=task_holder_id(task), holder_session=task.claimed_by_session)

        task.fail(reason="test: cancelled by a third party", by_holder=False)

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        held = occupancy_holder(self.fresh_worktree())
        assert held is not None
        assert held.holder == task_holder_id(task)
        with pytest.raises(WorktreeOccupiedError):
            acquire(self.fresh_worktree(), holder="task:999999", holder_session="")

    def test_third_party_fail_of_a_dead_holders_claim_releases_it(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        task.claim(claimed_by="worker-1", claimed_by_session="sess-1")
        acquire(self.worktree, holder=task_holder_id(task), holder_session=task.claimed_by_session)

        with mock.patch.object(singleton_mod, "pid_alive", return_value=False):
            task.fail(reason="test: cancelled by a third party", by_holder=False)

        assert occupancy_holder(self.fresh_worktree()) is None

    def test_the_release_rolls_back_with_a_failed_fsm_advance(self) -> None:
        """Forcing ``_advance_ticket`` to raise inside ``complete()``'s atomic block.

        Must leave the occupancy claim STILL held — proving the release is
        genuinely coupled to the SAME transaction as the status write, not a
        fire-and-forget side effect that already committed by the time the
        FSM advance fails.
        """
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        task.claim(claimed_by="worker-1", claimed_by_session="sess-1")
        acquire(self.worktree, holder=task_holder_id(task), holder_session=task.claimed_by_session)

        with (
            mock.patch.object(Task, "_advance_ticket", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            task.complete(result_artifact_path="/tmp/result.json")

        held = occupancy_holder(self.fresh_worktree())
        assert held is not None
        assert held.holder == task_holder_id(task)
        task.refresh_from_db()
        assert task.status != Task.Status.COMPLETED


class TestAParkedConversationTheRetryCannotContinueStartsFresh(TestCase):
    """souliane/teatree#4874: tasks 4973/4975 grew on Sonnet 5, then resumed as Opus 5.5 into a wall."""

    def setUp(self) -> None:
        ticket = Ticket.objects.create()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))

    def _parked(self, **attempt: object) -> str:
        TaskAttempt.objects.create(
            task=self.task,
            agent_session_id=_FAKE_UUID,
            exit_code=1,
            **{"num_turns": 162, "model": "claude-opus-5-5", "selected_model": "claude-opus-5-5", **attempt},
        )
        return self.task.continuation_on_requeue()

    def test_a_conversation_the_cli_fallback_served_is_not_resumed(self) -> None:
        assert self._parked(model="claude-sonnet-5", model_fell_back=True) == Task.SessionContinuation.FRESH

    def test_a_conversation_with_no_room_left_for_another_prompt_is_not_resumed(self) -> None:
        assert self._parked(context_tokens=313_014, context_window_tokens=350_000) == Task.SessionContinuation.FRESH

    def test_a_conversation_on_its_own_model_with_room_left_is_resumed(self) -> None:
        assert self._parked(context_tokens=313_014, context_window_tokens=1_000_000) == Task.SessionContinuation.SELF

    def test_a_router_resolving_its_handle_to_a_concrete_model_is_not_a_fallback(self) -> None:
        """The metered lane records the router's pick as the model; that is no reason to drop its thread."""
        routed = self._parked(model="qwen/qwen3-coder", selected_model="orcarouter/teatree-factory")

        assert routed == Task.SessionContinuation.SELF
