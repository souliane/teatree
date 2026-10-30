"""A Slack reply answers a queued question only when the owner wrote it."""

from unittest.mock import PropertyMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.core.models import DmContext, OperatorIdentity, PendingChatInjection, Session, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_binding import bind_reply
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from tests._owner_channel import OWNER_SLACK_ID
from tests.teatree_loop.slack_answer.test_cycle_question_binding import CountingReader, RecordingBackend
from tests.teatree_loop.test_question_binding import FakeMessaging, _question, _statement

_CHANNEL = "D-user"
_SOMEONE_ELSE = "U2"


def _reply(text: str, *, author: str, thread_ts: str = "", channel: str = _CHANNEL) -> PendingChatInjection:
    row = PendingChatInjection.record(
        channel=channel, slack_ts="400.0", text=text, context=DmContext(user_id=author, thread_ts=thread_ts)
    )
    assert row is not None
    return row


def _scan(backend: FakeMessaging) -> FakeMessaging:
    AskUserQuestionReplyScanner(backend=backend, overlay="", reader=_statement).scan()
    return backend


class TestOnlyTheOwnersReplyBinds(TestCase):
    def _operator_question(self, *, channel: str = _CHANNEL) -> DeferredQuestion:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        ticket = Ticket.objects.create(requested_by="github:alice")
        question = _question("Ship it?", slack_ts="100.0")
        question.slack_channel = channel
        question.task_session = Session.objects.create(ticket=ticket)
        question.save(update_fields=["slack_channel", "task_session"])
        return question

    def test_registered_requester_can_bind_own_question_but_owner_cannot(self) -> None:
        question = self._operator_question(channel="D-other")
        operator_reply = _reply(f"#{question.pk} yes", author="U-OP", channel="D-other")
        owner_reply = PendingChatInjection.record(
            channel="D-other",
            slack_ts="401.0",
            text=f"#{question.pk} yes",
            context=DmContext(user_id=OWNER_SLACK_ID),
        )

        assert bind_reply(operator_reply, reader=_statement, backend=FakeMessaging()) is not None
        assert owner_reply is not None
        assert bind_reply(owner_reply, reader=_statement, backend=FakeMessaging()) is None

    def test_owner_can_answer_card_delivered_before_requester_was_recorded(self) -> None:
        question = self._operator_question()
        reply = _reply(f"#{question.pk} yes", author=OWNER_SLACK_ID)
        backend = FakeMessaging()

        assert bind_reply(reply, reader=_statement, backend=backend) is not None

    def test_a_registered_operator_cannot_bind_another_operators_question(self) -> None:
        question = self._operator_question()
        bob = get_user_model().objects.create_superuser(username="bob", email="", password="unused")
        OperatorIdentity.objects.create(user=bob, platform="github", handle="bob", external_id="456")
        OperatorIdentity.objects.create(user=bob, platform="slack", handle="U-BOB")

        reply = _reply(f"#{question.pk} yes", author="U-BOB")

        assert bind_reply(reply, reader=_statement, backend=FakeMessaging()) is None

    def test_reactive_cycle_applies_registered_requesters_answer(self) -> None:
        question = self._operator_question()
        _reply("yes", author="U-OP", thread_ts="100.0")

        report = run_slack_answer_cycle(messaging_resolver=lambda _o: RecordingBackend(), reader=CountingReader())

        question.refresh_from_db()
        assert report.answered_question == 1
        assert question.answer_text == "yes"

    def _assert_unbound(self, question: DeferredQuestion, backend: FakeMessaging, reply: PendingChatInjection) -> None:
        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.is_pending
        assert question.resolved_via == DeferredQuestion.ResolvedVia.UNRESOLVED
        assert backend.react_calls == []
        assert reply.loop_replied_at is None

    def test_a_reply_by_someone_else_binds_nothing_on_the_id_rung(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        _question("Ship the other one?", slack_ts="200.0", generation=2)
        reply = _reply(f"#{question.pk} yes", author=_SOMEONE_ELSE)
        self._assert_unbound(question, _scan(FakeMessaging()), reply)

    def test_a_reply_by_someone_else_binds_nothing_on_the_thread_rung(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        reply = _reply("yes", author=_SOMEONE_ELSE, thread_ts="100.0")
        self._assert_unbound(question, _scan(FakeMessaging()), reply)

    def test_a_reply_by_someone_else_binds_nothing_on_the_sole_question_rung(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        reply = _reply("yes", author=_SOMEONE_ELSE)
        self._assert_unbound(question, _scan(FakeMessaging()), reply)

    def test_a_reply_with_no_recorded_author_binds_nothing(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        reply = _reply("yes", author="")
        self._assert_unbound(question, _scan(FakeMessaging()), reply)

    def test_an_empty_owner_id_never_matches_an_empty_author(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        reply = _reply("yes", author="")
        self._assert_unbound(question, _scan(FakeMessaging(user_id="")), reply)

    def test_a_backend_without_an_owner_id_binds_nothing(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        reply = _reply("yes", author="")
        backend = FakeMessaging()
        with patch.object(FakeMessaging, "user_id", new_callable=PropertyMock, side_effect=AttributeError("user_id")):
            _scan(backend)
        self._assert_unbound(question, backend, reply)

    def test_an_unreadable_owner_id_binds_nothing(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        reply = _reply("yes", author=OWNER_SLACK_ID)
        backend = FakeMessaging()
        with patch.object(FakeMessaging, "user_id", new_callable=PropertyMock, side_effect=RuntimeError("unreadable")):
            _scan(backend)
        self._assert_unbound(question, backend, reply)

    def test_an_id_reply_in_another_dm_binds_nothing(self) -> None:
        question = _question("Ratify?", slack_ts="100.0")
        reply = PendingChatInjection.record(
            channel="D-other-overlay",
            slack_ts="400.0",
            text=f"#{question.pk} approve",
            overlay="other",
            context=DmContext(user_id="U9"),
        )
        assert reply is not None
        backend = FakeMessaging(user_id="U9")
        AskUserQuestionReplyScanner(backend=backend, overlay="other", reader=_statement).scan()
        self._assert_unbound(question, backend, reply)

    def test_a_backend_with_no_owner_configured_binds_nothing(self) -> None:
        _question("Ship it?", slack_ts="100.0")
        reply = _reply("yes", author="")
        assert bind_reply(reply, reader=_statement, backend=NoopMessagingBackend()) is None

    def test_the_owners_reply_binds_and_is_recorded_as_slack(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        _reply(f"#{question.pk} yes", author=OWNER_SLACK_ID)
        backend = _scan(FakeMessaging())
        question.refresh_from_db()
        assert question.answer_text == "yes"
        assert question.resolved_via == DeferredQuestion.ResolvedVia.SLACK
        assert backend.react_calls != []

    def test_the_reactive_cycle_binds_nothing_for_someone_elses_reply(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        _reply("yes", author=_SOMEONE_ELSE, thread_ts="100.0")
        report = run_slack_answer_cycle(messaging_resolver=lambda _o: RecordingBackend(), reader=CountingReader())
        question.refresh_from_db()
        assert report.answered_question == 0
        assert question.is_pending
