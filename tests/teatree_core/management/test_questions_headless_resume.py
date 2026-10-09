"""``t3 teatree questions answer`` never resumes a parked task (#5096).

A command-line answer is never an owner channel, so it cannot authorize a resume: an
owner question is refused with its Slack route and left pending, and an internal one is
answered without minting a task. Only the owner's own answer to an owner question
resumes, and only while its subject is still open.
"""

import io

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.modelkit.owner_decision import OWNER_ANSWER_ROUTE
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from tests._owner_channel import OWNER_DECISION, answer_on_slack
from tests.factories import planned_ticket


def _parked_task(ticket: Ticket) -> Task:
    return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="shipping")


def _minted_tasks(parked: Task) -> list[Task]:
    return list(Task.objects.exclude(pk=parked.pk))


class TestACommandLineAnswerResumesNothing(TestCase):
    def test_cli_answer_on_owner_row_is_refused_and_mints_no_task(self) -> None:
        parked = _parked_task(planned_ticket())
        row = DeferredQuestion.record("Rotate the deploy token?", parked_task=parked, **OWNER_DECISION)
        stderr = io.StringIO()

        with pytest.raises(SystemExit) as refused:
            call_command("questions", "answer", row.pk, "rotate it", stderr=stderr)

        assert refused.value.code == 2
        assert OWNER_ANSWER_ROUTE in stderr.getvalue()
        row.refresh_from_db()
        assert row.is_pending
        assert row.resolved_via == DeferredQuestion.ResolvedVia.UNRESOLVED
        assert _minted_tasks(parked) == []

    def test_an_owner_id_among_also_ids_refuses_every_id(self) -> None:
        parked = _parked_task(planned_ticket())
        internal = DeferredQuestion.record("Which runner image?", parked_task=parked)
        owner = DeferredQuestion.record("Rotate the deploy token?", parked_task=parked, **OWNER_DECISION)

        with pytest.raises(SystemExit):
            call_command("questions", "answer", internal.pk, "the slim one", also=[owner.pk], stderr=io.StringIO())

        assert set(DeferredQuestion.pending().values_list("pk", flat=True)) == {internal.pk, owner.pk}

    def test_answering_internal_parked_question_mints_no_task(self) -> None:
        parked = _parked_task(planned_ticket())
        row = DeferredQuestion.record("Ship now, or wait until the review HOLD clears?", parked_task=parked)

        call_command("questions", "answer", row.pk, "ship now")

        row.refresh_from_db()
        assert row.answer_text == "ship now"
        assert row.resolved_via == DeferredQuestion.ResolvedVia.LOCAL
        assert _minted_tasks(parked) == []

    def test_dismissing_parked_question_mints_no_task(self) -> None:
        parked = _parked_task(planned_ticket())
        row = DeferredQuestion.record("Ship now, or wait until the review HOLD clears?", parked_task=parked)

        call_command("questions", "dismiss", row.pk, reason="the HOLD was withdrawn")

        row.refresh_from_db()
        assert row.dismissed_at is not None
        assert _minted_tasks(parked) == []


class TestAnOwnerAnswerAfterTheSubjectFinished(TestCase):
    def test_owner_answer_on_finished_subject_mints_no_task(self) -> None:
        ticket = planned_ticket()
        parked = _parked_task(ticket)
        row = DeferredQuestion.record("Rotate the deploy token?", parked_task=parked, **OWNER_DECISION)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)

        answer_on_slack(row, "rotate it")

        row.refresh_from_db()
        assert row.answered_on_owner_channel
        assert _minted_tasks(parked) == []
