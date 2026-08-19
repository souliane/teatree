"""The reactive cycle answers a queued question before it reads anything.

The cycle wakes ~1s after an inbound Slack event; the reply scanner runs on the
slower tick. So the cycle reaches nearly every owner reply first — and while it
had zero references to ``DeferredQuestion`` it classified the reply, stamped
``loop_replied_at``, reacted, and left the binder a queue that no longer held
the row. The owner's answer was acknowledged and dropped, every time.

The rung under test is FIRST in ``_process_unit``: bind, apply, ✅, return —
ahead of the routing read. An option pick therefore costs no model turn at all,
and free text costs the single reading the #1174 question-guard needs, shared
with the router rather than paid twice.
"""

import hashlib
import json
from dataclasses import dataclass, field

import pytest

from teatree.core.models import DmContext, PendingChatInjection, Task
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loop.slack_answer.vocabulary import InboundReaction
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "D1"


@dataclass
class RecordingBackend:
    """Records reactions and thread posts; the Slack network is the only fake."""

    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = (channel, ts)
        return {}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = (channel, thread_ts)
        return []

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}


@dataclass
class CountingReader:
    """An ``InboundReader`` that records every call — a stand-in for the model turn."""

    calls: list[str] = field(default_factory=list)

    def __call__(self, text: str) -> InboundReading:
        self.calls.append(text)
        return InboundReading(
            intent=InboundIntent.INSTRUCTION,
            answerable=False,
            work_summary="do the thing",
            source=ReadingSource.HEURISTIC,
        )


_OPTIONS_JSON = json.dumps([{"label": "Yes"}, {"label": "No"}], sort_keys=True, ensure_ascii=False)


def _question(text: str, *, slack_ts: str, generation: int = 1) -> DeferredQuestion:
    return DeferredQuestion.record(
        text,
        options_json=_OPTIONS_JSON,
        options_hash=hashlib.sha256(_OPTIONS_JSON.encode("utf-8")).hexdigest(),
        session_id="s",
        run_id="r",
        generation=generation,
        slack_channel=_CHANNEL,
        slack_ts=slack_ts,
    )


def _reply(text: str, *, slack_ts: str, thread_ts: str = "") -> PendingChatInjection:
    row = PendingChatInjection.record(
        channel=_CHANNEL,
        slack_ts=slack_ts,
        text=text,
        context=DmContext(user_id="U1", thread_ts=thread_ts),
    )
    assert row is not None
    return row


class TestCycleAnswersItsBoundQuestion:
    def test_thread_reply_resolves_the_question_without_reading_it(self) -> None:
        question = _question("Ship it?", slack_ts="100.0")
        reply = _reply("2", slack_ts="400.0", thread_ts="100.0")
        backend = RecordingBackend()
        reader = CountingReader()

        report = run_slack_answer_cycle(messaging_resolver=lambda _o: backend, reader=reader)

        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.answer_text == "No", "the fast consumer consumed the reply without binding it"
        assert question.resolved_via == "slack"
        assert reader.calls == [], "an option pick was routed through a model turn it did not need"
        assert reply.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY
        assert report.answered_question == 1

    def test_free_text_thread_reply_binds_on_one_reading(self) -> None:
        """The #1174 question-guard still reads free text — once, not twice."""
        question = _question("Which DB host?", slack_ts="100.0")
        _reply("use postgres-1", slack_ts="400.0", thread_ts="100.0")
        reader = CountingReader()

        run_slack_answer_cycle(messaging_resolver=lambda _o: RecordingBackend(), reader=reader)

        question.refresh_from_db()
        assert question.answer_text == "use postgres-1"
        assert reader.calls == ["use postgres-1"]

    def test_bound_reply_is_acked_done_and_dispatches_no_lane(self) -> None:
        _question("Ship it?", slack_ts="100.0")
        _reply("2", slack_ts="400.0", thread_ts="100.0")
        backend = RecordingBackend()

        run_slack_answer_cycle(messaging_resolver=lambda _o: backend, reader=CountingReader())

        assert (_CHANNEL, "400.0", InboundReaction.DONE) in backend.reactions
        assert backend.replies == [], "a bound answer posted prose the owner did not need"
        assert not Task.objects.exists(), "an answered question still minted a lane"

    def test_two_threaded_replies_in_one_window_answer_their_own_questions(self) -> None:
        """A unit binds on its lead's thread, so two threads must be two units."""
        first = _question("Ship it?", slack_ts="100.0")
        second = _question("Ship the other one?", slack_ts="200.0", generation=2)
        _reply("1", slack_ts="400.0", thread_ts="100.0")
        _reply("2", slack_ts="401.0", thread_ts="200.0")

        run_slack_answer_cycle(messaging_resolver=lambda _o: RecordingBackend(), reader=CountingReader())

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.answer_text == "Yes"
        assert second.answer_text == "No", "a reply in another thread was coalesced into the first question"

    def test_unbound_reply_still_falls_through_to_the_routing_read(self) -> None:
        _question("Which DB host?", slack_ts="100.0")
        _question("Ship the release?", slack_ts="300.0", generation=2)
        _reply("use postgres-1", slack_ts="400.0")
        backend = RecordingBackend()
        reader = CountingReader()

        report = run_slack_answer_cycle(messaging_resolver=lambda _o: backend, reader=reader)

        assert reader.calls == ["use postgres-1"], "the ambiguous reply was neither bound nor read"
        assert report.answered_question == 0
        assert DeferredQuestion.pending().count() == 2
