"""``tasks record-attempt`` — the sub-agent result-envelope hand-off.

Pins that a claimed task's structured result envelope drives it to its terminal
state through the SHARED recorder: the schema-key check, the #1284 phase-evidence
gate, then ``complete`` (auto-advancing the ticket) or ``fail``.

Every call passes ``--claim-token``: the token is what proves the caller still holds
the generation it was dispatched under, and a test that omits it exits on the token
guard before reaching the behaviour it names.
"""

import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from teatree.core.models.task_claim import claim_generation
from teatree.utils.git import run_strict
from tests.factories import WorktreeFactory, planned_ticket
from tests.teatree_core._self_review_helpers import author_ticket, self_review_result
from tests.teatree_core.conftest import record_maker_review_for_test
from tests.teatree_core.models._shared import _init_repo_with_branch

_HAND_PLAN = {
    "plan_text": "Implement the checkout fence and prove its refusal.",
    "base_sha": "a" * 40,
    "adequacy": {
        "design": {"content": "fence before the harness opens"},
        "integration_seams": {"content": ["src/teatree/agents/runner.py"]},
        "edge_cases": {"none_reason": "the refusal parks without changing the checkout"},
        "test_strategy": {"content": "run a claimed coding task through the real fence"},
        "acceptance_criteria": {"content": ["a foreign checkout state parks the task"]},
    },
}


class TestRecordAttemptCommand(TestCase):
    def _claim_next(self, task: Task, checkout: Path) -> str:
        WorktreeFactory(ticket=task.ticket, extra={"worktree_path": str(checkout)})
        claimed = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=claimed)
        [entry] = json.loads(claimed.getvalue())
        assert entry["task_id"] == task.pk
        return entry["claim_token"]

    def test_loop_plan_schedules_one_coding_task_even_on_replay(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, overlay="test")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="planning")
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp) / "checkout"
            _init_repo_with_branch(checkout, branch="feature", commits_ahead=0)
            token = self._claim_next(task, checkout)
            with patch(
                "teatree.core.worktree.occupancy.read_process_table", return_value=ProcessTable(frozenset(), "/proc")
            ):
                call_command(
                    "tasks",
                    "record-attempt",
                    str(task.pk),
                    json.dumps(_HAND_PLAN),
                    claim_token=token,
                    stdout=StringIO(),
                )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLAN_RECORDED
        assert task.attempts.latest("pk").result.get("recording_source") == "loop"
        assert Task.objects.filter(ticket=ticket, phase="coding").count() == 1
        assert task._apply_phase_transition() is False
        assert Task.objects.filter(ticket=ticket, phase="coding").count() == 1

    def test_loop_review_schedules_shipping_once_even_on_replay(self) -> None:
        ticket = author_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp) / "checkout"
            _init_repo_with_branch(checkout, branch="feature", commits_ahead=1)
            head = run_strict(repo=str(checkout), args=["rev-parse", "HEAD"])
            token = self._claim_next(task, checkout)
            record_maker_review_for_test(ticket, head)
            with (
                patch.object(Ticket, "has_shippable_diff", return_value=True),
                patch(
                    "teatree.core.worktree.occupancy.read_process_table",
                    return_value=ProcessTable(frozenset(), "/proc"),
                ),
            ):
                call_command(
                    "tasks",
                    "record-attempt",
                    str(task.pk),
                    json.dumps(self_review_result("merge_safe", reviewed_sha=head)),
                    claim_token=token,
                    stdout=StringIO(),
                )
                task.refresh_from_db()
                ticket.refresh_from_db()
                assert task.status == Task.Status.COMPLETED, task.attempts.latest("pk").error
                assert ticket.state == Ticket.State.SELF_REVIEWED
                task._apply_phase_transition()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SELF_REVIEWED
        assert Task.objects.filter(ticket=ticket, phase="shipping").count() == 1
        assert not DeferredQuestion.objects.filter(task_session=task.session).exists()

    def test_loop_review_hold_schedules_one_rework_even_on_replay(self) -> None:
        ticket = author_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp) / "checkout"
            _init_repo_with_branch(checkout, branch="feature", commits_ahead=0)
            token = self._claim_next(task, checkout)
            with patch(
                "teatree.core.worktree.occupancy.read_process_table", return_value=ProcessTable(frozenset(), "/proc")
            ):
                call_command(
                    "tasks",
                    "record-attempt",
                    str(task.pk),
                    json.dumps(self_review_result("hold")),
                    claim_token=token,
                    stdout=StringIO(),
                )
        task._apply_phase_transition()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        assert Task.objects.filter(ticket=ticket, phase="coding").count() == 1
        assert not DeferredQuestion.objects.filter(task_session=task.session).exists()

    def test_manual_hand_plan_keeps_the_writing_task_unscheduled(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, overlay="test")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="planning")
        task.claim(claimed_by="operator")
        call_command(
            "tasks",
            "record-attempt",
            str(task.pk),
            json.dumps(_HAND_PLAN),
            claim_token=claim_generation(task),
            stdout=StringIO(),
        )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLAN_RECORDED
        assert task.attempts.latest("pk").result["recording_source"] == "hand"
        assert not Task.objects.filter(ticket=ticket, phase="coding").exists()

    def test_manual_hand_review_hold_keeps_rework_unscheduled(self) -> None:
        ticket = author_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        task.claim(claimed_by="operator")
        call_command(
            "tasks",
            "record-attempt",
            str(task.pk),
            json.dumps(self_review_result("hold")),
            claim_token=claim_generation(task),
            stdout=StringIO(),
        )
        task._apply_phase_transition()
        assert task.attempts.latest("pk").result["recording_source"] == "hand"
        assert not Task.objects.filter(ticket=ticket, phase="coding").exists()

    def test_a_hand_record_with_a_process_in_the_checkout_pages_nobody(self) -> None:
        ticket = author_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        task.claim(claimed_by="operator")
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp) / "checkout"
            _init_repo_with_branch(checkout, branch="feature", commits_ahead=1)
            WorktreeFactory(ticket=ticket, extra={"worktree_path": str(checkout)})
            busy = ProcessTable(frozenset({checkout}), "/proc")
            with (
                patch("teatree.core.worktree.occupancy.read_process_table", return_value=busy),
                patch("teatree.core.worktree.occupancy.page_surviving_checkout") as page,
            ):
                call_command(
                    "tasks",
                    "record-attempt",
                    str(task.pk),
                    json.dumps(self_review_result("hold")),
                    claim_token=claim_generation(task),
                    stdout=StringIO(),
                )
        page.assert_not_called()

    def test_manual_hand_review_merge_safe_keeps_shipping_unscheduled(self) -> None:
        ticket = author_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        task.claim(claimed_by="operator")
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp) / "checkout"
            _init_repo_with_branch(checkout, branch="feature", commits_ahead=1)
            head = run_strict(repo=str(checkout), args=["rev-parse", "HEAD"])
            WorktreeFactory(ticket=ticket, extra={"worktree_path": str(checkout)})
            record_maker_review_for_test(ticket, head)
            with patch.object(Ticket, "has_shippable_diff", return_value=True):
                call_command(
                    "tasks",
                    "record-attempt",
                    str(task.pk),
                    json.dumps(self_review_result("merge_safe", reviewed_sha=head)),
                    claim_token=claim_generation(task),
                    stdout=StringIO(),
                )
        assert task.attempts.latest("pk").result["recording_source"] == "hand"
        assert not Task.objects.filter(ticket=ticket, phase="shipping").exists()

    def _claimed_task(self, *, phase: str = "coding") -> Task:
        ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.PLAN_RECORDED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_records_success_envelope_and_completes_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]})
        out = StringIO()

        call_command(
            "tasks",
            "record-attempt",
            str(task.pk),
            result_json,
            claim_token=claim_generation(task),
            agent_session_id="sess-1",
            stdout=out,
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        attempt = task.attempts.latest("pk")
        assert attempt.result["summary"] == "done"
        assert attempt.agent_session_id == "sess-1"
        assert task.ticket.state == Ticket.State.CODED
        # souliane/teatree#657: an in-session sub-agent always rides the
        # user's Max subscription seat — never the metered lane.
        assert attempt.lane == "subscription"

    def test_outage_death_fails_task_without_advancing_ticket(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps(
            {"summary": "Unable to connect to API", "files_modified": [{"path": "a.py", "action": "modified"}]},
        )

        call_command(
            "tasks", "record-attempt", str(task.pk), result_json, claim_token=claim_generation(task), stdout=StringIO()
        )

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.ticket.state == Ticket.State.PLAN_RECORDED
        assert task.attempts.latest("pk").error.startswith("outage_death:")

    def test_missing_phase_evidence_fails_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "no files changed"})

        # A valid-but-evidence-light envelope is recorded as a FAILED attempt
        # (a clean refusal, not a CLI error), so the command exits 0.
        call_command(
            "tasks", "record-attempt", str(task.pk), result_json, claim_token=claim_generation(task), stdout=StringIO()
        )

        task.refresh_from_db()
        # coding requires files_modified evidence (#1284) → fail, not complete.
        assert task.status == Task.Status.FAILED

    def test_needs_user_input_completes_and_records_a_deferred_question(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "blocked", "needs_user_input": True, "user_input_reason": "design call"})
        call_command(
            "tasks", "record-attempt", str(task.pk), result_json, claim_token=claim_generation(task), stdout=StringIO()
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        # There is no terminal to ask at, so the STOP parks as a durable question.
        assert DeferredQuestion.objects.filter(parked_task=task).exists()

    def test_invalid_json_rejected(self) -> None:
        task = self._claimed_task()
        with pytest.raises(SystemExit):
            call_command(
                "tasks",
                "record-attempt",
                str(task.pk),
                "not json",
                claim_token=claim_generation(task),
                stderr=StringIO(),
            )
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    def test_unexpected_keys_fail_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "ok", "bogus": 1})
        call_command(
            "tasks", "record-attempt", str(task.pk), result_json, claim_token=claim_generation(task), stdout=StringIO()
        )
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_unclaimed_task_rejected(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        # PENDING (not claimed) — recording must be refused.
        with pytest.raises(SystemExit):
            call_command(
                "tasks",
                "record-attempt",
                str(task.pk),
                json.dumps({"summary": "x"}),
                claim_token="never-claimed",
                stderr=StringIO(),
            )
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_finished_task_rejected(self) -> None:
        task = self._claimed_task()
        token = claim_generation(task)
        task.complete()
        with pytest.raises(SystemExit):
            call_command(
                "tasks",
                "record-attempt",
                str(task.pk),
                json.dumps({"summary": "x"}),
                claim_token=token,
                stderr=StringIO(),
            )


class TestLateRecordCannotFinishAnotherGeneration(TestCase):
    """A run whose lease lapsed mid-flight must not terminalize the unit that replaced it.

    Recording LATE — after the lease lapsed, the sweep re-offered the unit and a second
    tick claimed it — must leave the live generation and its ticket untouched.
    """

    def _claimed_task(self) -> Task:
        ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.PLAN_RECORDED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.claim(claimed_by="tick-1")
        return task

    @staticmethod
    def _lapse_and_reoffer(task: Task) -> None:
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=5))
        assert Task.objects.reclaim_orphaned_claims() == 1
        Task.objects.get(pk=task.pk).claim(claimed_by="tick-2")

    def test_late_success_does_not_complete_the_generation_tick_two_owns(self) -> None:
        task = self._claimed_task()
        stale_token = claim_generation(task)
        self._lapse_and_reoffer(task)
        envelope = json.dumps({"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]})

        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), envelope, claim_token=stale_token, stderr=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "tick-2"
        assert not task.attempts.exists()
        assert task.ticket.state == Ticket.State.PLAN_RECORDED

    def test_late_failure_does_not_fail_the_generation_tick_two_owns(self) -> None:
        task = self._claimed_task()
        stale_token = claim_generation(task)
        self._lapse_and_reoffer(task)
        # An evidence-light coding envelope: the recorder's FAIL path, not its complete one.
        envelope = json.dumps({"summary": "nothing changed"})

        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), envelope, claim_token=stale_token, stderr=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "tick-2"

    def test_the_live_generation_records_normally(self) -> None:
        task = self._claimed_task()
        self._lapse_and_reoffer(task)
        live = Task.objects.get(pk=task.pk)
        envelope = json.dumps({"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]})

        call_command(
            "tasks", "record-attempt", str(live.pk), envelope, claim_token=claim_generation(live), stdout=StringIO()
        )

        live.refresh_from_db()
        assert live.status == Task.Status.COMPLETED
        assert live.ticket.state == Ticket.State.CODED

    def test_an_omitted_token_is_refused_rather_than_recorded_unguarded(self) -> None:
        task = self._claimed_task()

        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), json.dumps({"summary": "x"}), stderr=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
