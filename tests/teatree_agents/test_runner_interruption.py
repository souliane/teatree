"""An interruption must not throw away what the run had already produced (#4464).

Observed on a live reviewing task: the agent emitted its verdict envelope, its row was
completed out from under it, and the interruption recorder wrote a summary-only attempt over
the already-COMPLETED row. The verdict was gone, ``review status`` still reported the previous
head, and from the outside that is indistinguishable from a reviewer that silently declined to
record — the shape that made souliane/teatree#4308 so hard to pin down. WHICH path completed
the row is a separate question: the issue's own correction re-attributes those two losses to
souliane/teatree#4465's orphan sweep (22s from creation to reaped, far inside any lease), not
to the starved lease this branch also widens. The discard is the same either way, which is why
it is pinned on its own rather than as a corollary of the lease fix.

The no-op-over-a-completed-row decision itself (#4100) is unchanged and pinned next door in
``test_runner_lease_loss.py``; what is pinned here is that the produced envelope rides along.
"""

import json

from django.core.management import call_command
from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage
from teatree.agents.runner import HarnessOutcome, _outcome_failure
from teatree.agents.runner_interruption import CeilingSalvage, _record_stuck_outcome
from teatree.agents.runner_usage import DispatchProvenance
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Session, Task, TaskAttempt, Ticket

_REVIEWED_HEAD = "a1b2c3d4" * 5
_ROW_COMPLETED = "lease lost for task 1: the row is already completed — the attempt has nothing left to hand over"

_VERDICT_ENVELOPE = {
    "summary": "cold review complete: merge_safe",
    "review_verdict": {
        "verdict": "merge_safe",
        "reviewed_sha": _REVIEWED_HEAD,
        "reviewer_identity": "cold-reviewer",
    },
}


def _interrupted(agent_text: str) -> HarnessOutcome:
    return HarnessOutcome(agent_text=agent_text, result_message=None, stuck_reason=_ROW_COMPLETED, lease_lost=True)


class TestAnInterruptedRunKeepsWhatItProduced(TestCase):
    def _completed_reviewing_task(self) -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url="https://github.com/o/r/pull/7",
            extra={"reviewed_sha": _REVIEWED_HEAD},
        )
        session = Session.objects.create(ticket=ticket, agent_id="reviewing")
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.CLAIMED)
        Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED, claimed_by="")
        return task

    def test_the_verdict_the_run_emitted_is_persisted_not_discarded(self) -> None:
        task = self._completed_reviewing_task()

        attempt = _outcome_failure(task, _interrupted(json.dumps(_VERDICT_ENVELOPE)), phase="reviewing")

        assert attempt is not None
        assert attempt.result["review_verdict"] == _VERDICT_ENVELOPE["review_verdict"]

    def test_interruption_does_not_persist_raw_skill_application_text(self) -> None:
        task = self._completed_reviewing_task()
        envelope = {
            **_VERDICT_ENVELOPE,
            "skill_application": [{"skill": "review", "evidence": "private/secret-path"}],
        }

        attempt = _outcome_failure(task, _interrupted(json.dumps(envelope)), phase="reviewing")

        assert attempt is not None
        assert "skill_application" not in attempt.result
        assert "secret-path" not in str(attempt.result)

    def test_the_interruption_is_named_alongside_the_runs_own_summary(self) -> None:
        task = self._completed_reviewing_task()

        attempt = _outcome_failure(task, _interrupted(json.dumps(_VERDICT_ENVELOPE)), phase="reviewing")

        assert attempt is not None
        summary = str(attempt.result["summary"])
        assert str(_VERDICT_ENVELOPE["summary"]) in summary
        assert _ROW_COMPLETED in summary

    def test_a_completed_row_is_never_left_with_nothing_persisted(self) -> None:
        # The blunt form of the acceptance: whatever the interrupted run produced is
        # readable off the row afterwards, so "completed with nothing behind it" is
        # unreachable whenever the run produced anything at all.
        task = self._completed_reviewing_task()

        _outcome_failure(task, _interrupted(json.dumps(_VERDICT_ENVELOPE)), phase="reviewing")

        recorded = TaskAttempt.objects.filter(task=task).values_list("result", flat=True)
        assert any("review_verdict" in result for result in recorded)

    def test_a_run_that_produced_nothing_records_the_interruption_alone(self) -> None:
        task = self._completed_reviewing_task()

        attempt = _outcome_failure(task, _interrupted(""), phase="reviewing")

        assert attempt is not None
        assert str(attempt.result["summary"]) == f"the row had already completed — {_ROW_COMPLETED}"

    def test_the_completed_row_is_still_left_alone(self) -> None:
        task = self._completed_reviewing_task()

        attempt = _outcome_failure(task, _interrupted(json.dumps(_VERDICT_ENVELOPE)), phase="reviewing")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.exit_code == 0
        assert attempt.error == ""
        assert task.status == Task.Status.COMPLETED


class TestACutShortRunKeepsNothingOverACompletedRow(TestCase):
    def test_a_row_a_rival_completed_is_left_to_the_interruption_recorder(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)
        envelope = {"summary": "implemented", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
        outcome = HarnessOutcome(agent_text=json.dumps(envelope), result_message=None, stuck_reason="runtime ceiling")

        kept = CeilingSalvage(phase="coding", lane="", provenance=DispatchProvenance()).kept(task, outcome)

        assert kept is None
        assert not TaskAttempt.objects.filter(task=task).exists()


class TestALeaseLossAfterAnOperatorCancel(TestCase):
    def test_lease_loss_after_operator_cancel_keeps_the_cancel(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/o/r/issues/4834")
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        stale = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.CLAIMED)
        call_command("tasks", "cancel", stale.pk, confirm=True)
        lease_lost = HarnessOutcome(
            agent_text="", result_message=None, stuck_reason="lease lost for task", lease_lost=True
        )

        attempt = _record_stuck_outcome(
            stale, lease_lost, stuck_reason="lease lost for task", usage=AttemptUsage(input_tokens=42)
        )

        row = Task.objects.get(pk=stale.pk)
        assert row.failure_kind == FailureKind.CANCELLED
        assert attempt.error.startswith("cancelled: ")
        assert attempt.input_tokens == 42
