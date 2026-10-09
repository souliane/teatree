"""A backlog sweep completes on a PERSISTED run, never on a self-reported count (#162 Rule 4).

Rule 4 says each sweep records how many tickets it had to change, and the factory
is healthy when that number tends to zero. A count the sweeping agent types into
its own envelope measures nothing — the agent that skipped the sweep and the one
that swept a clean backlog both write ``0``. So the envelope names a
:class:`~teatree.core.models.TicketSweepRun` the sweep actually opened and closed,
and this recorder refuses everything else: an absent run id, an id nobody began,
a run still open, and a count that disagrees with the URLs the facade recorded.

The presence half is the phase evidence gate; the truthfulness half is here.
"""

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.envelope_refusal import MALFORMED_TICKET_SWEEP_PREFIX, is_recorder_refusal
from teatree.agents.result_schema import check_evidence, required_evidence_for_phase
from teatree.agents.ticket_sweep_recorder import verify_returned_ticket_sweep
from teatree.core.models import Session, Task, Ticket, TicketSweepRun


def _sweep_task() -> Task:
    ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="sweeper")
    task = Task.objects.create(ticket=ticket, session=session, phase="backlog_sweep")
    task.claim(claimed_by="loop-slot")
    return task


def _finished_run(*, changed: tuple[str, ...] = ()) -> TicketSweepRun:
    run = TicketSweepRun.objects.begin(source="loop", overlay="acme")
    for url in changed:
        TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=url)
    return TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=len(changed) + 3)


class TestThePhaseGate:
    """``backlog_sweep`` may not complete on prose plus a summary."""

    def test_the_phase_requires_the_sweep_evidence_key(self) -> None:
        assert required_evidence_for_phase("backlog_sweep") == ("ticket_sweep",)

    def test_a_summary_only_sweep_is_refused(self) -> None:
        error = check_evidence({"summary": "swept the backlog"}, "backlog_sweep")
        assert "ticket_sweep" in error


class TestTheRunMustExistAndBeFinished(TestCase):
    """A run id is evidence only when it names a row the sweep really closed."""

    def test_a_finished_run_is_accepted(self) -> None:
        run = _finished_run(changed=("https://example.test/-/issues/1",))
        assert verify_returned_ticket_sweep({"ticket_sweep": {"run_id": run.run_id, "changed_count": 1}}) == ""

    def test_an_unknown_run_id_is_refused(self) -> None:
        error = verify_returned_ticket_sweep({"ticket_sweep": {"run_id": "deadbeef", "changed_count": 0}})
        assert error.startswith(MALFORMED_TICKET_SWEEP_PREFIX)
        assert "deadbeef" in error

    def test_an_unfinished_run_is_refused(self) -> None:
        run = TicketSweepRun.objects.begin(source="loop", overlay="acme")
        error = verify_returned_ticket_sweep({"ticket_sweep": {"run_id": run.run_id}})
        assert error.startswith(MALFORMED_TICKET_SWEEP_PREFIX)
        assert "finish" in error

    def test_a_blank_run_id_is_refused(self) -> None:
        error = verify_returned_ticket_sweep({"ticket_sweep": {"changed_count": 0}})
        assert error.startswith(MALFORMED_TICKET_SWEEP_PREFIX)

    def test_a_non_mapping_envelope_is_refused(self) -> None:
        assert verify_returned_ticket_sweep({"ticket_sweep": "run-7"}).startswith(MALFORMED_TICKET_SWEEP_PREFIX)


class TestTheCountIsMeasuredNotReported(TestCase):
    """The persisted URL set is the count; a disagreeing envelope is the fabrication."""

    def test_a_count_that_disagrees_with_the_run_is_refused(self) -> None:
        run = _finished_run(changed=("https://example.test/-/issues/1", "https://example.test/-/issues/2"))
        error = verify_returned_ticket_sweep({"ticket_sweep": {"run_id": run.run_id, "changed_count": 0}})
        assert error.startswith(MALFORMED_TICKET_SWEEP_PREFIX)
        assert "2" in error

    def test_a_zero_change_run_is_a_result_not_an_absence(self) -> None:
        run = _finished_run()
        assert verify_returned_ticket_sweep({"ticket_sweep": {"run_id": run.run_id, "changed_count": 0}}) == ""

    def test_an_omitted_count_defers_to_the_run(self) -> None:
        run = _finished_run(changed=("https://example.test/-/issues/9",))
        assert verify_returned_ticket_sweep({"ticket_sweep": {"run_id": run.run_id}}) == ""


class TestTheRefusalIsCorrectable(TestCase):
    """A refused sweep earns the one-shot corrective retry, not a page."""

    def test_the_refusal_classifies_as_a_recorder_refusal(self) -> None:
        error = verify_returned_ticket_sweep({"ticket_sweep": {"run_id": "nope"}})
        assert is_recorder_refusal(error)

    def test_a_fabricated_run_fails_the_whole_attempt(self) -> None:
        task = _sweep_task()
        record_result_envelope(task, {"summary": "swept", "ticket_sweep": {"run_id": "nope", "changed_count": 0}})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_a_real_run_completes_the_attempt(self) -> None:
        run = _finished_run()
        task = _sweep_task()
        record_result_envelope(task, {"summary": "swept", "ticket_sweep": {"run_id": run.run_id}})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestOtherPhasesAreUnaffected(TestCase):
    """An absent key is a no-op everywhere else — no new refusal for an overlay that never sweeps."""

    def test_an_absent_key_is_a_no_op(self) -> None:
        assert verify_returned_ticket_sweep({"summary": "coded"}) == ""


class TestOnlyTheSweepPhaseIsGated:
    """The key is required on ``backlog_sweep`` alone — no other phase gains a refusal."""

    @pytest.mark.parametrize("phase", ["coding", "reviewing", "planning", "shipping", "retro"])
    def test_no_other_phase_requires_the_key(self, phase: str) -> None:
        assert "ticket_sweep" not in required_evidence_for_phase(phase)
