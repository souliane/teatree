from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import DmContext, PendingChatInjection
from teatree.loop.slack_answer.coalescing import _Unit
from teatree.loop.slack_answer.delivery import _handle_simple
from teatree.loop.slack_answer.simple_answer import NEEDS_WORK_SENTINEL
from teatree.types import RawAPIDict
from tests._owner_channel import OWNER_SLACK_ID
from tests.teatree_loop.slack_answer.test_cycle import RecordingBackend

_BUILD = "teatree.loop.slack_answer.delivery.build_simple_answer"


def _row(ts: str = "1.0") -> PendingChatInjection:
    row = PendingChatInjection.record(
        channel="C1", slack_ts=ts, text="what's the status?", context=DmContext(user_id=OWNER_SLACK_ID)
    )
    assert row is not None
    return row


class LatestReplyBackend(RecordingBackend):
    """Stamps the bot reply after every coalesced message, as Slack does."""

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        self.thread_replies.setdefault(ts, []).append({"ts": "9.0", "user": "UBOT", "text": text, "thread_ts": ts})
        return {"ok": True, "ts": "9.0"}


class TestHandleSimple(TestCase):
    def setUp(self) -> None:
        self.backend = RecordingBackend()
        self.delivered: list[_Unit] = []

    def deliver(self, unit: _Unit) -> str:
        return _handle_simple(self.backend, unit, on_done=lambda _backend, done: self.delivered.append(done))

    def test_confirmed_post_stamps_every_row_and_runs_on_done(self) -> None:
        unit = _Unit([_row()])

        with patch(_BUILD, return_value="all green"):
            outcome = self.deliver(unit)

        unit.lead.refresh_from_db()
        assert outcome == "simple"
        assert self.backend.replies == [("C1", "1.0", "all green")]
        assert unit.lead.answer_kind == PendingChatInjection.AnswerKind.SIMPLE
        assert unit.lead.loop_response_confirmed_at is not None
        assert self.delivered == [unit]

    def test_no_cheap_answer_delegates_without_posting_or_claiming(self) -> None:
        for ts, answer in (("1.0", None), ("2.0", NEEDS_WORK_SENTINEL)):
            unit = _Unit([_row(ts)])

            with patch(_BUILD, return_value=answer):
                outcome = self.deliver(unit)

            unit.lead.refresh_from_db()
            assert outcome == "needs_work"
            assert unit.lead.loop_replied_at is None
        assert self.backend.replies == []
        assert self.delivered == []

    def test_lost_claim_retries_without_posting(self) -> None:
        unit = _Unit([_row()])

        with (
            patch(_BUILD, return_value="all green"),
            patch.object(PendingChatInjection, "mark_loop_replied", return_value=False),
            patch.object(PendingChatInjection, "claim_stale_loop_reply", return_value=False),
        ):
            outcome = self.deliver(unit)

        assert outcome == "retry"
        assert self.backend.replies == []
        assert self.delivered == []

    def test_unconfirmed_read_back_releases_the_claim_for_retry(self) -> None:
        self.backend.read_after_post = False
        unit = _Unit([_row()])

        with patch(_BUILD, return_value="all green"):
            outcome = self.deliver(unit)

        unit.lead.refresh_from_db()
        assert outcome == "retry"
        assert len(self.backend.replies) == 1
        assert unit.lead.loop_replied_at is None
        assert unit.lead.loop_response_confirmed_at is None
        assert self.delivered == []

    def test_post_failure_releases_the_claim_and_raises(self) -> None:
        self.backend.post_reply_raises = True
        unit = _Unit([_row()])

        with patch(_BUILD, return_value="all green"), self.assertRaisesMessage(RuntimeError, "slack 503"):
            self.deliver(unit)

        unit.lead.refresh_from_db()
        assert unit.lead.loop_replied_at is None
        assert self.delivered == []

    def test_reply_already_in_the_thread_is_confirmed_not_posted_twice(self) -> None:
        self.backend.thread_replies["1.0"] = [{"ts": "1.5", "user": "UBOT", "text": "all green", "thread_ts": "1.0"}]
        unit = _Unit([_row()])

        with patch(_BUILD, return_value="all green"):
            outcome = self.deliver(unit)

        unit.lead.refresh_from_db()
        assert outcome == "simple"
        assert self.backend.replies == []
        assert unit.lead.loop_response_confirmed_at is not None

    def test_reply_older_than_the_last_coalesced_message_does_not_confirm_it(self) -> None:
        self.backend = LatestReplyBackend()
        self.backend.thread_replies["1.0"] = [{"ts": "2.0", "user": "UBOT", "text": "old", "thread_ts": "1.0"}]
        unit = _Unit([_row("1.0"), _row("3.0")])

        with patch(_BUILD, return_value="all green"):
            outcome = self.deliver(unit)

        assert outcome == "simple"
        assert self.backend.replies == [("C1", "1.0", "all green")]
        for row in unit.rows:
            row.refresh_from_db()
            assert row.loop_response_confirmed_at is not None
