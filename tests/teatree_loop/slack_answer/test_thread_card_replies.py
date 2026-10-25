"""Owner card replies through the recorder and answer cycle."""

from dataclasses import dataclass, field, replace

from django.test import TestCase

from teatree.core.modelkit.question_card import CardOption
from teatree.core.models import DeferredQuestion, PendingChatInjection
from teatree.core.question_heal import live_owner_questions
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.types import RawAPIDict
from tests._owner_channel import OWNER_CARD, OWNER_SLACK_ID
from tests.teatree_loop.slack_answer.test_cycle import RecordingBackend as ThreadBackend

CHANNEL = "D1"


@dataclass
class Backend(ThreadBackend):
    updates: list[dict[str, object]] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    read_error: bool = False

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": "UBOT", "bot_id": "B_BOT_SELF"}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        self.reads.append(thread_ts)
        if self.read_error:
            message = "Slack read refused"
            raise RuntimeError(message)
        return super().fetch_thread_replies(channel=channel, thread_ts=thread_ts)

    def update_message(self, *, channel: str, ts: str, text: str, blocks: list[RawAPIDict]) -> RawAPIDict:
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True}


def _reader(text: str) -> InboundReading:
    return InboundReading(
        intent=InboundIntent.INSTRUCTION,
        answerable=False,
        work_summary=text,
        source=ReadingSource.HEURISTIC,
    )


def _card(ts: str) -> DeferredQuestion:
    card = replace(
        OWNER_CARD,
        options=(CardOption("Approve", "I go ahead.", recommended=True), CardOption("Decline", "I leave it.")),
    )
    return DeferredQuestion.record("Approve the change?", slack_channel=CHANNEL, slack_ts=ts, card=card)


def _event(
    backend: Backend, *, ts: str, root: str, text: str = "approve", user: str = OWNER_SLACK_ID
) -> PendingChatInjection | None:
    raw: RawAPIDict = {"ts": ts, "thread_ts": root, "user": user, "channel": CHANNEL, "text": text}
    SlackDmRecorder(backend=backend).record_events([raw])
    return PendingChatInjection.objects.filter(slack_ts=ts).first()


def _cycle(backend: Backend):
    return run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_reader)


class TestThreadCardReplies(TestCase):
    def test_nested_card_reply_binds_by_thread_membership(self) -> None:
        card = _card("200.2")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["200.1"] = [{"ts": "200.1"}, {"ts": "200.2", "thread_ts": "200.1"}]
        reply = _event(backend, ts="200.3", root="200.1")
        assert reply is not None

        report = _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        assert card.answer_text == "Approve"
        assert reply.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY
        assert not live_owner_questions()
        assert report.answered_question == 1
        assert backend.reads == ["200.1"]

    def test_root_card_with_bot_reminders_replays_raw_owner_reply(self) -> None:
        decoy = _card("250.1")
        card = _card("300.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["300.1"] = [
            {"ts": "300.1"},
            {"ts": "300.2", "user": "UBOT"},
            {"ts": "300.3", "user": "UBOT"},
        ]
        reply = _event(backend, ts="300.4", root="300.1")
        assert reply is not None, "recorder dropped the raw reply"

        _cycle(backend)

        card.refresh_from_db()
        decoy.refresh_from_db()
        reply.refresh_from_db()
        assert card.resolved_via == DeferredQuestion.ResolvedVia.SLACK
        assert reply.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY
        assert card.answer_text == "Approve", "the bound answer did not use the card's option label"
        assert decoy.is_pending
        assert backend.reads == []

    def test_two_cards_in_one_thread_get_visible_choice_then_id_answer(self) -> None:
        first = _card("400.2")
        second = _card("400.3")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["400.4"] = {"ts": "400.4", "thread_ts": "400.1"}
        backend.thread_replies["400.1"] = [
            {"ts": "400.1"},
            {"ts": "400.2"},
            {"ts": "400.3"},
        ]
        first_reply = _event(backend, ts="400.4", root="400.1")
        assert first_reply is not None

        report = _cycle(backend)

        first.refresh_from_db()
        second.refresh_from_db()
        first_reply.refresh_from_db()
        assert first.is_pending
        assert second.is_pending
        assert first_reply.answer_kind == PendingChatInjection.AnswerKind.ACK, report.errors
        assert any(f"#{first.pk}" in text and f"#{second.pk}" in text for _, _, text in backend.replies)

        _event(backend, ts="400.5", root="400.1", text=f"#{second.pk} approve")
        _cycle(backend)
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.is_pending
        assert second.answer_text == "Approve"

    def test_root_and_nested_card_in_one_thread_also_require_a_choice(self) -> None:
        root = _card("450.1")
        nested = _card("450.2")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["450.3"] = {"ts": "450.3", "thread_ts": "450.1"}
        backend.thread_replies["450.1"] = [{"ts": "450.1"}, {"ts": "450.2", "thread_ts": "450.1"}]
        reply = _event(backend, ts="450.3", root="450.1")
        assert reply is not None

        _cycle(backend)

        root.refresh_from_db()
        nested.refresh_from_db()
        reply.refresh_from_db()
        assert root.is_pending
        assert nested.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.ACK
        assert any(f"#{root.pk}" in text and f"#{nested.pk}" in text for _, _, text in backend.replies)

    def test_decision_log_names_the_root_and_member_keys(self) -> None:
        _card("470.1")
        _card("480.2")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["480.1"] = [{"ts": "480.1"}, {"ts": "480.2", "thread_ts": "480.1"}]
        _event(backend, ts="470.2", root="470.1")
        _event(backend, ts="480.3", root="480.1")

        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            _cycle(backend)

        messages = " ".join(logs.output)
        assert "key=root" in messages
        assert "key=thread-member" in messages

    def test_closed_card_thread_gets_visible_note_without_binding_decoy(self) -> None:
        closed = _card("500.1")
        closed.apply_answer("Yes", resolved_via="local")
        decoy = _card("510.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["500.2"] = {"ts": "500.2", "thread_ts": "500.1"}
        backend.thread_replies["500.1"] = [{"ts": "500.1"}]
        reply = _event(backend, ts="500.2", root="500.1")
        assert reply is not None

        _cycle(backend)

        decoy.refresh_from_db()
        assert decoy.is_pending
        assert any(ts == "500.2" and "card" in text.lower() for _, ts, text in backend.replies)

    def test_unrelated_thread_keeps_ordinary_routing_and_does_not_bind_decoy(self) -> None:
        decoy = _card("900.1")
        other_channel = DeferredQuestion.record(
            "Other channel card?", slack_channel="D2", slack_ts="800.1", card=OWNER_CARD
        )
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["800.1"] = [{"ts": "800.1"}]
        backend.message_meta["900.2"] = {"ts": "900.2", "thread_ts": "800.1"}
        reply = _event(backend, ts="900.2", root="800.1", text="Please check the build")
        assert reply is not None

        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            report = _cycle(backend)

        decoy.refresh_from_db()
        other_channel.refresh_from_db()
        assert decoy.is_pending
        assert other_channel.is_pending
        assert report.answered_question == 0
        assert "no pending card in this thread" in " ".join(logs.output)
        assert not any("card" in text.lower() for _, _, text in backend.replies)

    def test_thread_read_failure_retries_without_claiming(self) -> None:
        card = _card("600.2")
        backend = Backend(user_id=OWNER_SLACK_ID, read_error=True)
        reply = _event(backend, ts="600.3", root="600.1")
        assert reply is not None

        with self.assertLogs("teatree.loop.slack_answer.cycle", level="WARNING") as logs:
            report = _cycle(backend)

        reply.refresh_from_db()
        card.refresh_from_db()
        assert report.errors == 1
        assert reply.loop_replied_at is None
        assert card.is_pending
        assert "Slack read refused" in " ".join(logs.output)

    def test_non_owner_reply_is_rejected_and_logged(self) -> None:
        card = _card("700.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        _event(backend, ts="700.2", root="700.1", user="U_OTHER")

        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            _cycle(backend)

        card.refresh_from_db()
        assert card.is_pending
        assert "author is not the owner" in " ".join(logs.output)

    def test_api_app_post_stays_filtered(self) -> None:
        backend = Backend(user_id=OWNER_SLACK_ID)
        with self.assertLogs("teatree.loop.scanners.slack_dm_recorder", level="INFO") as logs:
            recorded = SlackDmRecorder(backend=backend).record_events(
                [
                    {
                        "ts": "800.2",
                        "thread_ts": "800.1",
                        "user": OWNER_SLACK_ID,
                        "channel": CHANNEL,
                        "text": "approve",
                        "api_app_id": "A_APP",
                    }
                ]
            )

        assert recorded == 0
        assert not PendingChatInjection.objects.filter(slack_ts="800.2").exists()
        assert "posted through an app token" in " ".join(logs.output)
