"""A Slack reply answers a queued question only when the owner wrote it."""

from unittest.mock import PropertyMock, patch

from django.test import TestCase

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.core.models import DmContext, PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_binding import bind_reply, configured_owner_id
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from tests._owner_channel import OWNER_SLACK_ID
from tests.teatree_loop.slack_answer.test_cycle_question_binding import CountingReader, RecordingBackend
from tests.teatree_loop.test_question_binding import FakeMessaging, _question, _statement

_CHANNEL = "D-user"
_SOMEONE_ELSE = "U2"


def _reply(text: str, *, author: str, thread_ts: str = "") -> PendingChatInjection:
    row = PendingChatInjection.record(
        channel=_CHANNEL, slack_ts="400.0", text=text, context=DmContext(user_id=author, thread_ts=thread_ts)
    )
    assert row is not None
    return row


def _scan(backend: FakeMessaging) -> FakeMessaging:
    AskUserQuestionReplyScanner(backend=backend, overlay="", reader=_statement).scan()
    return backend


class TestOnlyTheOwnersReplyBinds(TestCase):
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
        assert bind_reply(reply, reader=_statement, owner_user_id=configured_owner_id(NoopMessagingBackend())) is None

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
