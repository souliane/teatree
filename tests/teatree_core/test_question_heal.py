"""A question whose trigger has healed is withdrawn before anything surfaces it (#4904)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.provision.failure_question import provision_failure_marker, record_provision_failure_question
from teatree.core.question_heal import live_owner_questions, record_withheld, withdraw_healed, withhold
from teatree.loop.question_drain import drain_pending_questions
from tests._git_repo import make_git_repo
from tests._owner_channel import OWNER_CARD, OWNER_DECISION, answer_on_slack, owner_card
from tests.factories import planned_ticket


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
            "Did provisioning fail for the backend?",
            dedupe_marker=provision_failure_marker(ticket.pk),
            **OWNER_DECISION,
        )
        Worktree.objects.create(
            ticket=ticket, repo_path="backend", branch="x", extra={"worktree_path": str(make_git_repo(root / "b"))}
        )
        owner = DeferredQuestion.record(
            "Which DB host?", card=owner_card(OwnerDecision.PRODUCT_SCOPE, "the ticket names no host")
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


def _parked_task() -> Task:
    ticket = planned_ticket()
    return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="shipping")


class TestWithhold(TestCase):
    def test_a_pending_owner_row_moves_to_the_internal_queue_with_an_audit(self) -> None:
        row = DeferredQuestion.record("Can I ship the widgets?", dedupe_marker="ship:1", **OWNER_DECISION)

        assert withhold(row, ["the question carries internal shorthand (widget_count)"])

        row.refresh_from_db()
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.dedupe_marker == "ship:1"
        assert row.is_pending
        audit = DeferredQuestionAudit.objects.get(question=row, action="withheld")
        assert audit.resolver_id == "owner_card_check"
        assert "widget_count" in audit.note
        assert not DeferredQuestion.owner_pending().exists()

    def test_a_row_without_a_marker_gets_one_naming_itself(self) -> None:
        row = DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION)

        withhold(row, ["a problem"])

        row.refresh_from_db()
        assert row.dedupe_marker == f"withheld:{row.pk}"

    def test_withholding_twice_audits_once(self) -> None:
        row = DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION)

        assert withhold(row, ["a problem"])
        assert not withhold(row, ["a problem"])

        assert DeferredQuestionAudit.objects.filter(question=row, action="withheld").count() == 1

    def test_an_answered_row_is_never_withheld(self) -> None:
        row = DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION)
        answer_on_slack(row, "Yes")

        assert not withhold(row, ["a problem"])

        row.refresh_from_db()
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
        assert not DeferredQuestionAudit.objects.filter(action="withheld").exists()


class TestRecordWithheld(TestCase):
    def test_the_draft_is_an_internal_row_parked_on_its_task_with_an_audit(self) -> None:
        task = _parked_task()

        row = record_withheld(
            "Approve this reply?\n\nSee #42.",
            ["the quoted text carries internal shorthand (#42)"],
            parked_task=task,
            task_session=task.session,
            dedupe_marker="answer-draft:9",
        )

        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert (row.parked_task_id, row.task_session_id) == (task.pk, task.session_id)
        assert "#42" in DeferredQuestionAudit.objects.get(question=row, action="withheld").note

    def test_recording_it_again_adds_no_row_and_no_audit(self) -> None:
        task = _parked_task()
        kwargs = {"parked_task": task, "task_session": task.session, "dedupe_marker": "answer-draft:9"}

        first = record_withheld("Approve this reply?", ["p"], **kwargs)
        second = record_withheld("Approve this reply?", ["p"], **kwargs)

        assert second.pk == first.pk
        assert DeferredQuestionAudit.objects.filter(action="withheld").count() == 1


def _ship_question(task: Task | None = None) -> DeferredQuestion:
    return DeferredQuestion.record(
        "Can I ship the widgets?",
        dedupe_marker="ship:1",
        parked_task=task,
        task_session=task.session if task else None,
        card=OWNER_CARD,
    )


class TestAWithheldRowHandsItsWaitingTaskToTheCard(TestCase):
    def test_a_withheld_row_hands_its_waiting_task_to_the_card(self) -> None:
        task = _parked_task()
        old = _ship_question(task)
        withhold(old, ["a problem"])

        card_row = _ship_question()

        old.refresh_from_db()
        assert old.dismissed_reason == "superseded by an owner question"
        assert card_row.pk != old.pk
        assert card_row.audience == DeferredQuestion.Audience.OWNER_QUESTION
        assert (card_row.parked_task_id, card_row.task_session_id) == (task.pk, task.session_id)

    def test_answering_the_card_resumes_the_carried_task(self) -> None:
        task = _parked_task()
        withhold(_ship_question(task), ["a problem"])

        answer_on_slack(_ship_question(), "Yes")

        assert task.child_tasks.count() == 1

    def test_a_task_the_caller_names_wins_over_the_carried_one(self) -> None:
        named = _parked_task()
        withhold(_ship_question(_parked_task()), ["a problem"])

        assert _ship_question(named).parked_task_id == named.pk

    def test_a_marker_held_by_no_withheld_row_carries_nothing(self) -> None:
        DeferredQuestion.record("Is the repair stuck?", dedupe_marker="ship:1", parked_task=_parked_task())

        assert _ship_question().parked_task_id is None
