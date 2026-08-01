"""An inbound QUESTION is never consumed as the answer to a pending question.

The reported failure, reproduced at its source. A ``DeferredQuestion`` was live
in the owner's DM channel. The owner then asked his OWN question in the same
thread — "beside that everything fixed?" — and the reply scanner, which applies
any non-digit body verbatim, took it as the answer, claimed the row, and reacted
:white_check_mark:.

Two things went wrong at once and both are pinned here: the owner's question was
answered by nobody, and the completion emoji told him it had been handled. The
guard is a conservative one — a body the reader classifies as a QUESTION is left
for the ordinary DM path, which answers or dispatches it. Erring this way costs
one extra round trip on a genuinely-interrogative answer; erring the other way
loses the owner's question entirely.
"""

import hashlib
import json
from dataclasses import dataclass, field

import pytest

from teatree.core.models import PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "D-owner"
_OPTIONS = [{"label": "Yes"}, {"label": "No"}]


def _options_hash(options: list[dict]) -> str:
    blob = json.dumps(options, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class FakeMessaging:
    """Self-DM ``MessagingBackend``: every react/post is ungated (self surface)."""

    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    route_token: str = "self"

    def _is_self_dm(self, channel: str) -> bool:
        _ = channel
        return True

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {"ok": True}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/permalink"


def _record_question(*, slack_ts: str) -> DeferredQuestion:
    return DeferredQuestion.record(
        "Ship it?",
        options_json=json.dumps(_OPTIONS),
        options_hash=_options_hash(_OPTIONS),
        session_id="s",
        run_id="r",
        generation=1,
        slack_channel=_CHANNEL,
        slack_ts=slack_ts,
    )


def _record_reply(text: str, *, slack_ts: str) -> PendingChatInjection:
    row = PendingChatInjection.record(channel=_CHANNEL, slack_ts=slack_ts, text=text, user_id="U-owner")
    assert row is not None
    return row


def _reader_saying(intent: InboundIntent):
    return lambda _text: InboundReading(
        intent=intent,
        answerable=False,
        work_summary="",
        source=ReadingSource.MODEL,
        rationale="test fixture",
    )


class TestOwnerQuestionIsNotAnAnswer:
    def test_a_question_body_does_not_resolve_the_live_question(self) -> None:
        question = _record_question(slack_ts="1.0")
        reply = _record_reply("beside that everything fixed?", slack_ts="2.0")
        backend = FakeMessaging()

        AskUserQuestionReplyScanner(
            backend=backend,  # ty: ignore[invalid-argument-type]
            reader=_reader_saying(InboundIntent.QUESTION),
        ).scan()

        question.refresh_from_db()
        assert question.answered_at is None, "the owner's own question was applied as the answer"
        reply.refresh_from_db()
        assert reply.loop_replied_at is None, "the question was claimed and never answered"

    def test_a_question_body_is_never_marked_complete(self) -> None:
        _record_question(slack_ts="1.0")
        _record_reply("beside that everything fixed?", slack_ts="2.0")
        backend = FakeMessaging()

        AskUserQuestionReplyScanner(
            backend=backend,  # ty: ignore[invalid-argument-type]
            reader=_reader_saying(InboundIntent.QUESTION),
        ).scan()

        assert backend.react_calls == [], (
            f"an unanswered question was reacted to as handled: {backend.react_calls!r}"
        )

    def test_a_real_answer_still_resolves_the_question(self) -> None:
        """Anti-vacuity: the guard must not disable the scanner's actual job."""
        question = _record_question(slack_ts="1.0")
        _record_reply("Yes", slack_ts="2.0")
        backend = FakeMessaging()

        AskUserQuestionReplyScanner(
            backend=backend,  # ty: ignore[invalid-argument-type]
            reader=_reader_saying(InboundIntent.FYI),
        ).scan()

        question.refresh_from_db()
        assert question.answered_at is not None
        assert [emoji for _c, _ts, emoji in backend.react_calls] == ["white_check_mark"]

    def test_a_digit_reply_never_reaches_the_reader(self) -> None:
        """A digit selects an option — unambiguous, and never worth a model turn."""
        question = _record_question(slack_ts="1.0")
        _record_reply("2", slack_ts="2.0")
        backend = FakeMessaging()

        def _exploding_reader(_text: str) -> InboundReading:
            msg = "the reader must not be consulted for a digit option-pick"
            raise AssertionError(msg)

        AskUserQuestionReplyScanner(
            backend=backend,  # ty: ignore[invalid-argument-type]
            reader=_exploding_reader,
        ).scan()

        question.refresh_from_db()
        assert question.answer_text == "No"
