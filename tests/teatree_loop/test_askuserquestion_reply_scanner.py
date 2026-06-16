"""Match a Slack reply to its live ``DeferredQuestion`` and apply it (#1174).

The reply matcher is the second leg of the Slack→Claude bridge: it reads
unconsumed ``PendingChatInjection`` user replies, binds each to the
currently-live ``DeferredQuestion`` for that DM channel, applies the
answer, claims the row's ``loop_replied_at`` (so the reactive answer
cycle does NOT spawn an answerer), and reacts ✅ — verify-by-readback
before stamping, leave the row for retry on a readback failure. A reply
with no live question is left to the ordinary DM path (never forced into
a question result).
"""

import hashlib
import json
from dataclasses import dataclass, field

import pytest

from teatree.core.models import PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "D-user"
_OPTIONS = [{"label": "Yes"}, {"label": "No"}]


def _options_hash(options: list[dict]) -> str:
    blob = json.dumps(options, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class FakeMessaging:
    """Self-DM MessagingBackend: every react/post is ungated (self surface)."""

    permalink: str = "https://slack/permalink"
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    raise_on_react: bool = False
    route_token: str = "self"

    def _is_self_dm(self, channel: str) -> bool:
        _ = channel
        return True

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        if self.raise_on_react:
            msg = "react down"
            raise RuntimeError(msg)
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {"ok": True}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return self.permalink


def _record_question(*, generation: int, slack_ts: str, session_id: str = "s", run_id: str = "r") -> DeferredQuestion:
    return DeferredQuestion.record(
        "Ship it?",
        options_json=json.dumps(_OPTIONS),
        options_hash=_options_hash(_OPTIONS),
        session_id=session_id,
        run_id=run_id,
        generation=generation,
        slack_channel=_CHANNEL,
        slack_ts=slack_ts,
    )


def _record_reply(text: str, *, slack_ts: str) -> PendingChatInjection:
    row = PendingChatInjection.record(channel=_CHANNEL, slack_ts=slack_ts, text=text, user_id="U1")
    assert row is not None
    return row


def _scan(backend: FakeMessaging) -> None:
    AskUserQuestionReplyScanner(backend=backend, overlay="").scan()


class TestDigitReplyResolvesOption:
    def test_digit_one_maps_to_first_label_and_claims_row(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        reply = _record_reply("1", slack_ts="200.0")
        backend = FakeMessaging()

        _scan(backend)

        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.answer_text == "Yes"
        assert question.resolved_via == "slack"
        assert question.is_pending is False
        assert reply.loop_replied_at is not None
        assert reply.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY
        assert backend.react_calls == [(_CHANNEL, "200.0", "white_check_mark")]

    def test_digit_two_maps_to_second_label(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        _record_reply("2", slack_ts="200.0")

        _scan(FakeMessaging())

        question.refresh_from_db()
        assert question.answer_text == "No"


class TestFreeTextReply:
    def test_non_digit_applied_verbatim(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        _record_reply("use the staging cluster", slack_ts="200.0")

        _scan(FakeMessaging())

        question.refresh_from_db()
        assert question.answer_text == "use the staging cluster"


class TestOutOfRangeDigit:
    def test_digit_beyond_options_applied_verbatim(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        _record_reply("9", slack_ts="200.0")

        _scan(FakeMessaging())

        question.refresh_from_db()
        assert question.answer_text == "9"


class TestOptionsHashMismatch:
    def test_digit_with_stale_hash_treated_stale_no_wrong_label(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        question.options_hash = "deadbeef"
        question.save(update_fields=["options_hash"])
        reply = _record_reply("1", slack_ts="200.0")

        _scan(FakeMessaging())

        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.is_pending is True
        assert reply.loop_replied_at is None


class TestStaleCases:
    def test_answered_locally_first_leaves_reply_for_generic_path(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        question.apply_answer("local answer", resolved_via="local")
        reply = _record_reply("1", slack_ts="200.0")

        _scan(FakeMessaging())

        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.answer_text == "local answer"
        assert question.resolved_via == "local"
        assert reply.loop_replied_at is None

    def test_agent_advanced_prior_generation_is_stale(self) -> None:
        old = _record_question(generation=1, slack_ts="100.0")
        old.mark_stale("superseded by newer question")
        reply = _record_reply("1", slack_ts="200.0")

        _scan(FakeMessaging())

        old.refresh_from_db()
        reply.refresh_from_db()
        assert old.resolved_via == "stale"
        assert reply.loop_replied_at is None

    def test_reply_before_mirror_ts_does_not_bind(self) -> None:
        question = _record_question(generation=1, slack_ts="500.0")
        reply = _record_reply("1", slack_ts="400.0")

        _scan(FakeMessaging())

        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.is_pending is True
        assert reply.loop_replied_at is None


class TestDoubleReply:
    def test_second_reply_finds_nothing_live_and_is_left_alone(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        first = _record_reply("1", slack_ts="200.0")
        second = _record_reply("2", slack_ts="201.0")

        _scan(FakeMessaging())

        question.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        assert question.answer_text == "Yes"
        assert first.loop_replied_at is not None
        assert second.loop_replied_at is None


class TestReadbackFailureLeavesRow:
    def test_react_failure_leaves_reply_and_question_for_retry(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        reply = _record_reply("1", slack_ts="200.0")
        backend = FakeMessaging(raise_on_react=True)

        _scan(backend)

        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.is_pending is True
        assert reply.loop_replied_at is None

    def test_empty_permalink_leaves_reply_for_retry(self) -> None:
        question = _record_question(generation=1, slack_ts="100.0")
        reply = _record_reply("1", slack_ts="200.0")
        backend = FakeMessaging(permalink="")

        _scan(backend)

        question.refresh_from_db()
        reply.refresh_from_db()
        assert question.is_pending is True
        assert reply.loop_replied_at is None
