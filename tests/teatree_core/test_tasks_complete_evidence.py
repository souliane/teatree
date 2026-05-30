import io

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Task, TaskAttempt, Ticket

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class TestTasksCompleteEvidenceGate(TestCase):
    """Fail-closed artifact-evidence gate on ``tasks complete --note`` (#1280).

    A completion note that ASSERTS an external outcome (merged / posted /
    shipped / deployed) must carry a resolvable artifact pointer, or the
    completion is refused. A note with no outcome claim — the common internal
    path — is never gated.
    """

    def _claimed_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        return task

    def test_outcome_claim_without_pointer_refused_fail_closed(self) -> None:
        task = self._claimed_task()
        stderr = io.StringIO()

        with pytest.raises(SystemExit) as exc:
            call_command("tasks", "complete", task.pk, note="shipped the feature", stderr=stderr)

        assert exc.value.code == 1
        assert "no resolvable artifact pointer" in stderr.getvalue()
        # Fail-closed: the task is NOT completed and no attempt was recorded.
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert not TaskAttempt.objects.filter(task=task).exists()

    def test_outcome_claim_with_pointer_succeeds_and_records_evidence(self) -> None:
        task = self._claimed_task()

        call_command("tasks", "complete", task.pk, note="shipped via https://example.com/mr/77")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        attempt = TaskAttempt.objects.filter(task=task).first()
        assert attempt is not None
        assert attempt.result == {"complete_note": "shipped via https://example.com/mr/77"}

    def test_internal_completion_without_outcome_claim_still_succeeds(self) -> None:
        # Regression guard (load-bearing, non-breaking): an internal-progress
        # note with no outcome verb is never gated.
        task = self._claimed_task()

        call_command("tasks", "complete", task.pk, note="refactored the parser and split the module")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        attempt = TaskAttempt.objects.filter(task=task).first()
        assert attempt is not None
        assert attempt.result == {"complete_note": "refactored the parser and split the module"}

    def test_completion_with_no_note_still_succeeds(self) -> None:
        # The plainest internal path: no note at all is never gated.
        task = self._claimed_task()

        call_command("tasks", "complete", task.pk)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert not TaskAttempt.objects.filter(task=task).exists()
