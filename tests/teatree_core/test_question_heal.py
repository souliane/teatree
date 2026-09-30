"""A question whose trigger has healed is withdrawn before anything surfaces it (#4904)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.provision.failure_question import record_provision_failure_question
from teatree.core.question_heal import live_owner_questions, withdraw_healed
from tests._git_repo import make_git_repo


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
        healed = record_provision_failure_question(ticket, "failed to create worktrees for: backend")
        Worktree.objects.create(
            ticket=ticket, repo_path="backend", branch="x", extra={"worktree_path": str(make_git_repo(root / "b"))}
        )
        owner = DeferredQuestion.record("Which DB host?")
        DeferredQuestion.record("Repair stall", audience=DeferredQuestion.Audience.INTERNAL)

        assert live_owner_questions() == [owner]
        healed.refresh_from_db()
        assert not healed.is_pending
