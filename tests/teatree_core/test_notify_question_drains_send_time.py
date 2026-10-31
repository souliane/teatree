"""An owner card whose subject settled between record and send is dropped by every drain, never posted (#5096)."""

import datetime as dt
from collections.abc import Callable
from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion, DeferredQuestionAudit, PullRequest, Session, Ticket
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
)
from tests._owner_channel import OWNER_DECISION

_CHANNEL = "D0DEMOOWNER"


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


def _owner_card_on(ticket: Ticket, **fields: object) -> DeferredQuestion:
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return DeferredQuestion.record("May I post it?", task_session=session, **OWNER_DECISION, **fields)


def _sends() -> dict[str, Callable[[MagicMock], object]]:
    later = timezone.now() + dt.timedelta(hours=3)
    return {
        "first post": lambda backend: drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend),
        "resurface": lambda backend: drain_deferred_questions(user_id="U0DEMOOWNER", backend=backend),
        "bump": lambda backend: reask_escalated_questions(user_id="U0DEMOOWNER", backend=backend, now=later),
    }


class TestASettledSubjectIsNeverPosted(TestCase):
    def test_a_card_whose_ticket_merged_after_it_was_recorded_posts_nothing_in_any_drain(self) -> None:
        for name, send in _sends().items():
            with self.subTest(drain=name):
                DeferredQuestion.objects.all().delete()
                ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.CODED)
                mirrored = {} if name == "first post" else {"slack_channel": _CHANNEL, "slack_ts": "1700000000.000100"}
                card = _owner_card_on(ticket, **mirrored)
                Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)
                backend = _backend()

                send(backend)

                backend.post_message.assert_not_called()
                card.refresh_from_db()
                assert not card.is_pending
                assert DeferredQuestionAudit.objects.get(question=card, action="dismissed").resolver_id == (
                    "subject_terminal"
                )

    def test_a_card_whose_every_pull_request_closed_posts_nothing(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.PR_OPENED)
        pull_request = PullRequest.objects.create(
            ticket=ticket, url="https://github.com/acme/repo/pull/1", repo="acme/repo", iid="1", state="open"
        )
        card = _owner_card_on(ticket)
        PullRequest.objects.filter(pk=pull_request.pk).update(state=PullRequest.State.MERGED)
        backend = _backend()

        drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)

        backend.post_message.assert_not_called()
        card.refresh_from_db()
        assert not card.is_pending

    def test_a_card_whose_ticket_is_still_live_is_posted(self) -> None:
        card = _owner_card_on(Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.CODED))
        backend = _backend()

        drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)

        backend.post_message.assert_called_once()
        card.refresh_from_db()
        assert card.slack_ts
