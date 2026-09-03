"""A work-implying answering run that returns no ``work_item`` is REFUSED (#4527).

The filing half of this change is only reachable when the agent hands a ``work_item``
back, so the refusal is what makes the channel mandatory rather than optional. A reply
posted with the request dropped is indistinguishable from a reply posted with the
request filed — these tests are the only thing that can tell the two apart, and they
drive the real recorder rather than the predicate, so the refusal cannot be reverted
while the suite stays green.
"""

from unittest import mock

from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.envelope_refusal import is_recorder_refusal
from teatree.core.models import Session, Task, Ticket
from teatree.utils.url_slug import slack_conversation_anchor

_CHANNEL = "D-owner"
_ANSWER = {"text": "On it."}


def _answering_task(*, implies_work: bool) -> Task:
    ticket = Ticket.objects.create(
        issue_url=slack_conversation_anchor(channel=_CHANNEL, slack_ts="1.0"),
        overlay="t3-teatree",
        role=Ticket.Role.AUTHOR,
        state=Ticket.State.STARTED,
        short_description="detect the open-PR bottleneck",
        extra={
            "slack_answer": {
                "channel": _CHANNEL,
                "slack_ts": "1.0",
                "question": "the open-PR bottleneck must never recur",
                "fingerprint": "fp-bottleneck",
                "implies_work": implies_work,
            }
        },
    )
    session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
    task = Task.objects.create(ticket=ticket, session=session, phase="answering", subject="answer the owner")
    task.claim(claimed_by="loop-slot")
    return task


class TestAWorkImplyingRunMustSayWhatTheRequestBecomes(TestCase):
    """The refusal itself — the enforcement half of the ``work_item`` channel."""

    def test_an_answer_alone_is_refused_when_the_message_implied_work(self) -> None:
        task = _answering_task(implies_work=True)

        attempt = record_result_envelope(task, {"summary": "replied", "answer": dict(_ANSWER)}, phase="answering")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED, "the reply was recorded and the owner's request went nowhere"
        assert "work_item" in attempt.error, attempt.error

    def test_the_refusal_earns_the_corrective_retry_not_a_human_page(self) -> None:
        """Classified as a recorder refusal, so ``transient_requeue`` restates the contract."""
        task = _answering_task(implies_work=True)

        attempt = record_result_envelope(task, {"summary": "replied", "answer": dict(_ANSWER)}, phase="answering")

        assert is_recorder_refusal(attempt.error), attempt.error

    def test_a_returned_work_item_completes_the_run(self) -> None:
        """The anti-vacuity control: the same run passes the moment the request is placed."""
        task = _answering_task(implies_work=True)
        result = {
            "summary": "replied and filed",
            "answer": dict(_ANSWER),
            "work_item": {"existing_issue_url": "https://github.com/souliane/teatree/issues/4242"},
        }

        with mock.patch("teatree.agents.reactive_envelope_recorders.record_reactive_envelopes"):
            record_result_envelope(task, result, phase="answering")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_a_message_implying_no_work_is_answered_and_nothing_more_is_owed(self) -> None:
        """The gate is task-conditional: an ordinary answerable question owes no work item."""
        task = _answering_task(implies_work=False)

        with mock.patch("teatree.agents.reactive_envelope_recorders.record_reactive_envelopes"):
            record_result_envelope(task, {"summary": "replied", "answer": dict(_ANSWER)}, phase="answering")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_a_blocked_run_escalates_instead_of_being_refused(self) -> None:
        """``needs_user_input`` is a surfaced block, not a dropped request."""
        task = _answering_task(implies_work=True)
        result = {"summary": "blocked", "answer": dict(_ANSWER), "needs_user_input": True, "user_input_reason": "?"}

        with mock.patch("teatree.agents.reactive_envelope_recorders.record_reactive_envelopes"):
            record_result_envelope(task, result, phase="answering")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
