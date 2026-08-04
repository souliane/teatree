"""A lost lease does not overrule the phase's own completion evidence (#3982).

Observed on a single shipping task: it pushed its branch, opened the pull request and its
ticket advanced to ``in_review`` with the PR attached — then the row was recorded
``failed`` / ``lease_lost`` because the heartbeat's renewal lost the claim generation. The
work landed; only the bookkeeping said otherwise, and that false failure is exactly what
the auto-repair sweep reads as "re-do this".

These pin the split: a lease loss WITH landing evidence records the outcome the evidence
supports; a lease loss WITHOUT it still records the failure, unchanged.
"""

from django.test import TestCase

from teatree.agents.headless import HarnessOutcome, _outcome_failure
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.review_verdict import ReviewVerdict

_REVIEWED_HEAD = "a1b2c3d4" * 5
_ROW_COMPLETED = "lease lost for task 1: the row is already completed — the attempt has nothing left to hand over"


def _lease_lost(*, reason: str = "lease lost for task 1: re-claimed by a competing worker") -> HarnessOutcome:
    return HarnessOutcome(agent_text="", result_message=None, stuck_reason=reason, lease_lost=True)


def _watchdog_breach() -> HarnessOutcome:
    return HarnessOutcome(agent_text="", result_message=None, stuck_reason="turns ceiling exceeded: 500 > 200")


class _Dispatch(TestCase):
    def _task(self, *, phase: str = "shipping", state: str = Ticket.State.IN_REVIEW) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
        return self._claimed_task(ticket, phase=phase)

    def _reviewing_task(self, *, reviewed_sha: str = _REVIEWED_HEAD) -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url="https://github.com/o/r/pull/7",
            extra={"reviewed_sha": reviewed_sha},
        )
        return self._claimed_task(ticket, phase="reviewing")

    def _claimed_task(self, ticket: Ticket, *, phase: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase=phase,
            status=Task.Status.CLAIMED,
            claimed_by="worker-A",
        )


class TestLandedWorkIsNotRecordedFailed(_Dispatch):
    def test_a_shipping_task_that_opened_its_pr_is_not_failed(self) -> None:
        task = self._task()

        attempt = _outcome_failure(task, _lease_lost(), phase="shipping")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code == 0
        assert attempt.error == ""
        assert task.status != Task.Status.FAILED
        assert task.failure_kind != FailureKind.LEASE_LOST

    def test_the_recorded_attempt_carries_the_evidence_and_the_lease_loss(self) -> None:
        task = self._task()

        attempt = _outcome_failure(task, _lease_lost(), phase="shipping")

        assert attempt is not None
        summary = str(attempt.result["summary"])
        assert "in_review" in summary
        assert "lease" in summary

    def test_an_unowned_row_lands_completed_so_nothing_re_dispatches_it(self) -> None:
        # The in-process reclaim requeued the row PENDING and nobody took it up; leaving
        # it there re-dispatches work that already shipped.
        task = self._task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.PENDING, claimed_by="")

        _outcome_failure(task, _lease_lost(), phase="shipping")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_a_live_successors_claim_is_never_stolen(self) -> None:
        # A genuinely competing worker holds the row now. Recording OUR evidence must not
        # terminate the claim it is working under.
        task = self._task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.CLAIMED, claimed_by="worker-B")

        _outcome_failure(task, _lease_lost(), phase="shipping")

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "worker-B"

    def test_an_open_pull_request_is_evidence_even_when_the_state_lagged(self) -> None:
        task = self._task(state=Ticket.State.REVIEWED)
        PullRequest.objects.create(
            ticket=task.ticket,
            url="https://github.com/o/r/pull/7",
            repo="o/r",
            iid="7",
            state=PullRequest.State.OPEN,
        )

        attempt = _outcome_failure(task, _lease_lost(), phase="shipping")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code == 0
        assert task.status != Task.Status.FAILED


class TestACompletedRowIsNeverRecordedFailed(_Dispatch):
    """The row completed; the heartbeat noticed afterwards — that is a no-op, not a failure (#4100).

    The dominant recorded failure on the live box: the task reached COMPLETED, the still-running
    heartbeat's next renewal found the claim generation gone and the run was interrupted, and the
    interruption was written over a finished row as ``failed`` / ``lease_lost``.
    """

    def test_the_completed_row_survives_the_interruption(self) -> None:
        task = self._reviewing_task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED, claimed_by="")

        attempt = _outcome_failure(task, _lease_lost(reason=_ROW_COMPLETED), phase="reviewing")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code == 0
        assert attempt.error == ""
        assert task.status == Task.Status.COMPLETED
        assert task.failure_kind != FailureKind.LEASE_LOST

    def test_the_recorded_attempt_still_names_the_interruption(self) -> None:
        task = self._reviewing_task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED, claimed_by="")

        attempt = _outcome_failure(task, _lease_lost(reason=_ROW_COMPLETED), phase="reviewing")

        assert attempt is not None
        assert _ROW_COMPLETED in str(attempt.result["summary"])


class TestAReviewThatRecordedItsVerdictIsNotFailed(_Dispatch):
    """A recorded verdict at the reviewed head IS the reviewing phase's landed output (#4100)."""

    def _verdict(self, task: Task) -> None:
        ReviewVerdict.record(
            pr_id=7,
            slug="o/r",
            reviewed_sha=_REVIEWED_HEAD,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
            ticket=task.ticket,
        )

    def test_a_requeued_review_with_a_verdict_lands_completed(self) -> None:
        # The in-process reclaim requeued the row PENDING; leaving it there re-dispatches a
        # review that already produced its verdict for this head.
        task = self._reviewing_task()
        self._verdict(task)
        Task.objects.filter(pk=task.pk).update(status=Task.Status.PENDING, claimed_by="")

        attempt = _outcome_failure(task, _lease_lost(), phase="reviewing")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code == 0
        assert attempt.error == ""
        assert task.status == Task.Status.COMPLETED
        assert _REVIEWED_HEAD[:8] in str(attempt.result["summary"])

    def test_a_review_that_recorded_nothing_is_still_recorded_failed(self) -> None:
        task = self._reviewing_task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.PENDING, claimed_by="")

        attempt = _outcome_failure(task, _lease_lost(), phase="reviewing")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code != 0
        assert task.status == Task.Status.FAILED
        assert task.failure_kind == FailureKind.LEASE_LOST


class TestALeaseLossWithoutEvidenceStillFails(_Dispatch):
    def test_a_shipping_task_that_landed_nothing_is_recorded_failed(self) -> None:
        task = self._task(state=Ticket.State.REVIEWED)

        attempt = _outcome_failure(task, _lease_lost(), phase="shipping")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code != 0
        assert "stuck_loop: lease lost" in attempt.error
        assert task.status == Task.Status.FAILED
        assert task.failure_kind == FailureKind.LEASE_LOST

    def test_a_watchdog_breach_on_a_landed_phase_still_fails(self) -> None:
        # Only a LOST LEASE is evidence about the lease rather than the work. A runtime /
        # turns ceiling breach is a real runaway and must stay a failure.
        task = self._task()

        attempt = _outcome_failure(task, _watchdog_breach(), phase="shipping")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code != 0
        assert task.status == Task.Status.FAILED
