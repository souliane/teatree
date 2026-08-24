"""The ``no_result_envelope`` refusal must earn the one-shot corrective retry.

``_record_success`` refuses to record a headless run that emitted prose and no
JSON result envelope (``no_result_envelope: …``). That refusal is right — an
unparsable success is not evidence of success — but the harness already owns
the machinery that makes the contract SATISFIABLE: ``transient_requeue``'s
one-shot corrective retry, which reopens the task with an explicit
"emit the envelope" instruction appended to its prompt.

The runner's refusal string was never listed in the consumer's envelope-refusal
vocabulary, so the most literal omitted-envelope failure was the one class the
corrective retry never fired for: the first prose-only run parked the task and
paged a human. These tests pin the routing (retry once, then escalate) and the
phase-accuracy of the instruction; the producer/consumer parity that let the two
strings drift apart is pinned on the runner side, in
``tests/teatree_agents/test_runner_no_envelope_guard.py``.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR, corrective_instruction, is_envelope_refusal
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.transient_requeue import requeue_transient_failed


def _failed_task(*, phase: str, state: str = Ticket.State.STARTED) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)


def _add_failed_attempt(task: Task, *, error: str) -> None:
    TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=0,  # an envelope refusal is a clean REFUSAL, not a crash
        error=error,
    )
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)


class TestNoEnvelopeCorrectiveRetry(TestCase):
    def test_debugging_no_envelope_refusal_gets_one_corrective_retry(self) -> None:
        # ``debugging`` has no PHASE_REQUIRED_EVIDENCE entry, so a prose-only run
        # is refused by the RUNNER with ``no_result_envelope`` rather than by the
        # recorder's evidence gate. It must earn the same one-shot corrective
        # retry an omitted ``files_modified`` envelope earns — not an immediate page.
        task = _failed_task(phase="debugging")
        _add_failed_attempt(task, error=NO_ENVELOPE_ERROR)

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING
        assert "[auto-corrective-retry]" in task.execution_reason
        assert "envelope" in task.execution_reason.lower()
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_no_envelope_refusal_is_corrective_on_any_phase(self) -> None:
        # The refusal is a pure OUTPUT-FORMAT failure with a phase-independent
        # correction. Its reachable set is every phase that neither requires
        # evidence nor is prose-exempt: `debugging` (covered by the sibling test
        # above, and already inside _CORRECTIVE_PHASES) AND the phases outside
        # that pair — architectural_review, bughunt, e2e, the codex_* variants.
        # Gating it on the pair left this second group paging a human on the
        # very first prose-only run; `architectural_review` stands for it here.
        task = _failed_task(phase="architectural_review")
        _add_failed_attempt(task, error=NO_ENVELOPE_ERROR)

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_second_no_envelope_refusal_escalates_and_never_loops(self) -> None:
        # The retry is bounded at exactly ONE. A ticket can never retry
        # indefinitely on the same unsatisfiable condition: the second identical
        # refusal stops and surfaces.
        task = _failed_task(phase="debugging")
        _add_failed_attempt(task, error=NO_ENVELOPE_ERROR)
        assert requeue_transient_failed() == 1

        _add_failed_attempt(task, error=NO_ENVELOPE_ERROR)

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert "[repair-halt-parked]" in task.execution_reason
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1
        # Durably parked — a later sweep never resurrects another retry.
        assert requeue_transient_failed() == 0

    def test_a_fresh_task_row_earns_its_own_retry_after_an_earlier_ones_refusal(self) -> None:
        # #4075. The budget spans Task ROWS of one ticket-phase, so a re-dispatched task
        # inherits the earlier row's no-envelope attempt. Both carry the same CONSTANT
        # reason and so the same fingerprint — which used to read as a two-strikes stall
        # and page a human before this row had run its own single corrective retry.
        first = _failed_task(phase="debugging")
        _add_failed_attempt(first, error=NO_ENVELOPE_ERROR)
        assert requeue_transient_failed() == 1
        Task.objects.filter(pk=first.pk).update(status=Task.Status.COMPLETED)

        second = Task.objects.create(
            ticket=first.ticket,
            session=first.session,
            phase="debugging",
            status=Task.Status.FAILED,
        )
        _add_failed_attempt(second, error=NO_ENVELOPE_ERROR)

        assert requeue_transient_failed() == 1

        second.refresh_from_db()
        assert second.status == Task.Status.PENDING
        assert "[auto-corrective-retry]" in second.execution_reason
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 0

    def test_corrective_instruction_names_only_the_phases_own_required_keys(self) -> None:
        # The note lands in the re-dispatched prompt (``execution_reason`` →
        # "Reason:"), so naming a key the phase does not require teaches the
        # agent the wrong contract. ``debugging`` requires only ``summary``.
        assert "files_modified" in corrective_instruction("coding")
        assert "summary" in corrective_instruction("coding")
        assert "files_modified" not in corrective_instruction("debugging")
        assert "summary" in corrective_instruction("debugging")

    def test_a_genuine_defect_is_still_not_corrective_retried(self) -> None:
        # Control: the classifier must be able to say NO. A real assertion
        # failure is not an envelope refusal and still escalates on the first hit.
        assert not is_envelope_refusal("AssertionError: expected 3 got 4")
        task = _failed_task(phase="debugging")
        _add_failed_attempt(task, error="AssertionError: expected 3 got 4")

        assert requeue_transient_failed() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1
