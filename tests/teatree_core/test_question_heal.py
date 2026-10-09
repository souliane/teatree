"""A question whose trigger has healed is withdrawn before anything surfaces it (#4904)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.provision.failure_question import provision_failure_marker, record_provision_failure_question
from teatree.core.question_heal import live_owner_questions, withdraw_healed
from teatree.loop.question_drain import drain_pending_questions
from tests._git_repo import make_git_repo
from tests._owner_channel import OWNER_DECISION


class TestWithdrawHealed(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ticket = Ticket.objects.create(
            overlay="test", issue_url="https://example.com/issues/9", repos=[], state=Ticket.State.WORK_STARTED
        )
        self.question = record_provision_failure_question(self.ticket, "no repos on ticket", retries=6)

    def _heal(self) -> None:
        Ticket.objects.filter(pk=self.ticket.pk).update(repos=["backend"])
        Worktree.objects.create(
            ticket=self.ticket,
            repo_path="backend",
            branch="9-x",
            extra={"worktree_path": str(make_git_repo(self.root / "backend"))},
        )

    def test_a_healed_row_is_dismissed_and_left_out(self) -> None:
        unrelated = DeferredQuestion.record("Which DB host?")
        self._heal()

        live = withdraw_healed([self.question, unrelated])

        assert live == [unrelated]
        self.question.refresh_from_db()
        assert self.question.resolved_via == DeferredQuestion.ResolvedVia.STALE
        assert "backend" in self.question.dismissed_reason
        audit = DeferredQuestionAudit.objects.get(question=self.question, action="dismissed")
        assert audit.resolver_id == "provision_healed"

    def test_an_unhealed_row_is_kept_pending(self) -> None:
        assert withdraw_healed([self.question]) == [self.question]
        self.question.refresh_from_db()
        assert self.question.is_pending

    def test_withdrawing_twice_writes_one_audit(self) -> None:
        self._heal()

        withdraw_healed([self.question])
        withdraw_healed(DeferredQuestion.objects.filter(pk=self.question.pk))

        assert DeferredQuestionAudit.objects.filter(question=self.question, action="dismissed").count() == 1

    def test_an_answer_that_landed_first_is_never_overwritten(self) -> None:
        self._heal()
        self.question.apply_answer("ignore it", resolved_via=DeferredQuestion.ResolvedVia.SLACK)

        withdraw_healed([self.question])

        self.question.refresh_from_db()
        assert self.question.resolved_via == DeferredQuestion.ResolvedVia.SLACK
        assert self.question.dismissed_at is None


class TestLiveOwnerQuestions(TestCase):
    def test_internal_rows_are_excluded_and_healed_rows_withdrawn(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ticket = Ticket.objects.create(overlay="test", repos=["backend"], state=Ticket.State.WORK_STARTED)
        healed = DeferredQuestion.record(
            "Provision failed: backend", dedupe_marker=provision_failure_marker(ticket.pk), **OWNER_DECISION
        )
        Worktree.objects.create(
            ticket=ticket, repo_path="backend", branch="x", extra={"worktree_path": str(make_git_repo(root / "b"))}
        )
        owner = DeferredQuestion.record(
            "Which DB host?", decision=OwnerDecision.PRODUCT_SCOPE, checked=["the ticket names no host"]
        )
        DeferredQuestion.record("Repair stall")

        assert live_owner_questions() == [owner]
        healed.refresh_from_db()
        assert not healed.is_pending


class TestPhaseWedgeHealed(TestCase):
    def _wedge(self, state: str) -> tuple[Ticket, Task, DeferredQuestion]:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=state)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)
        task._apply_phase_transition()
        return ticket, task, DeferredQuestion.pending().get(dedupe_marker__startswith=f"fsm-wedge:{ticket.pk}:")

    def test_a_wedge_whose_ticket_moved_on_is_drained_and_a_live_one_kept(self) -> None:
        moved, _task, moved_row = self._wedge(Ticket.State.NOT_STARTED)
        _still, _still_task, still_row = self._wedge(Ticket.State.NOT_STARTED)
        Ticket.objects.filter(pk=moved.pk).update(state=Ticket.State.CODED)
        Task.objects.create(ticket=moved, session=Session.objects.create(ticket=moved), phase="testing")

        report = drain_pending_questions()

        assert report.drained == 1
        moved_row.refresh_from_db()
        still_row.refresh_from_db()
        assert not moved_row.is_pending
        assert DeferredQuestionAudit.objects.get(question=moved_row, action="dismissed").resolver_id == (
            "phase_wedge_healed"
        )
        assert still_row.is_pending

    def test_a_terminal_ticket_drains_its_wedge(self) -> None:
        ticket, _task, row = self._wedge(Ticket.State.NOT_STARTED)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.IGNORED)

        assert withdraw_healed([row]) == []

    def test_an_advanced_ticket_with_no_newer_task_keeps_its_wedge(self) -> None:
        ticket, _task, row = self._wedge(Ticket.State.NOT_STARTED)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.REVIEW_REQUESTED)

        assert withdraw_healed([row]) == [row]


class TestALegacyWedgeRowDrainsToo(TestCase):
    """Rows written before #5030 keyed the wedge on ``tool_use_id`` with no task part."""

    def _legacy_row(self, ticket: Ticket) -> DeferredQuestion:
        return DeferredQuestion.record(
            f"FSM wedge on ticket {ticket.pk}", tool_use_id=f"fsm-wedge:{ticket.pk}:planning"
        )

    def test_a_legacy_row_drains_once_its_ticket_moved_past_the_phase_and_a_wedged_one_stays(self) -> None:
        healed = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.PLAN_RECORDED)
        Task.objects.create(ticket=healed, session=Session.objects.create(ticket=healed), phase="coding")
        wedged = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        healed_row, wedged_row = self._legacy_row(healed), self._legacy_row(wedged)

        assert withdraw_healed([healed_row, wedged_row]) == [wedged_row]
        healed_row.refresh_from_db()
        assert not healed_row.is_pending
