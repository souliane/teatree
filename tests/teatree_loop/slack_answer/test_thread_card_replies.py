"""Owner card replies through the recorder and answer cycle."""

from dataclasses import dataclass, field, replace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion, OperatorIdentity, PendingChatInjection, Session, Ticket
from teatree.core.models.deferred_question import DeferredQuestionAudit
from teatree.core.question_heal import live_owner_questions
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.question_binding import cards_in_thread
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.loop.slack_answer.card_discussion import NOBODY_LISTENING, UNROUTED_AUDIT_ACTION
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
    auth_user_id: str = "UBOT"

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": self.auth_user_id, "bot_id": "B_BOT_SELF"}

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
        intent=InboundIntent.QUESTION if "?" in text else InboundIntent.INSTRUCTION,
        answerable=False,
        work_summary=text,
        source=ReadingSource.HEURISTIC,
    )


def _action_card(ts: str) -> DeferredQuestion:
    action = ActionRequest(kind=OwnerActionKind.VOID, target="99999", binding="digest")
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=("The checkout changed.",),
        blocker="Only the owner may accept this state.",
        why="The factory stopped before writing.",
        options=(CardOption("Accept", "I record this state.", recommended=True), CardOption("Decline", "It stays.")),
        action=action,
    )
    row = DeferredQuestion.record("Accept this checkout?", dedupe_marker=action_marker(action), card=card)
    assert row.mark_mirrored(channel=CHANNEL, slack_ts=ts)
    return row


def _bot_reminder(ts: str, root: str) -> RawAPIDict:
    return {
        "ts": ts,
        "thread_ts": root,
        "user": "UBOT",
        "bot_id": "B_BOT_SELF",
        "channel": CHANNEL,
        "text": "Still waiting for your answer.",
    }


def _api_owner_reply(ts: str, root: str, text: str = "approve") -> RawAPIDict:
    return {
        "type": "message",
        "channel_type": "im",
        "ts": ts,
        "thread_ts": root,
        "user": OWNER_SLACK_ID,
        "channel": CHANNEL,
        "text": text,
        "team": "T1",
        "app_id": "A_OWNER_SCRIPT",
        "api_app_id": "A_OWNER_SCRIPT",
        "parent_user_id": "UBOT",
        "blocks": [{"type": "rich_text", "elements": []}],
    }


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
    def _assert_unrouted_burst(self, size: int) -> None:
        card = _card("336.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        timestamps = [f"336.{index + 10}" for index in range(size)]
        for timestamp in timestamps:
            reply = _event(backend, ts=timestamp, root="336.1", text=f"Explain part {timestamp}?")
            assert reply is not None

        for _ in range(4):
            _cycle(backend)

        assert [text for _channel, _ts, text in backend.replies] == [NOBODY_LISTENING]
        assert set(card.audits.filter(action=UNROUTED_AUDIT_ACTION).values_list("note", flat=True)) == set(timestamps)
        assert (
            PendingChatInjection.objects.filter(slack_ts__in=timestamps, loop_replied_at__isnull=True).count() == size
        )

    def test_two_coalesced_unrouted_replies_get_one_notice_across_cycles(self) -> None:
        self._assert_unrouted_burst(2)

    def test_four_coalesced_unrouted_replies_get_one_notice_across_cycles(self) -> None:
        self._assert_unrouted_burst(4)

    def test_an_audit_on_another_channel_does_not_hide_a_new_reply(self) -> None:
        older_card = _card("599.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        for index in range(10):
            timestamp = f"599.{index + 10}"
            assert _event(backend, ts=timestamp, root="", text="Thanks for the update") is not None
            DeferredQuestionAudit.objects.create(
                question=older_card, action=UNROUTED_AUDIT_ACTION, note=timestamp, resolver_id="card_discussion"
            )
        other_card = DeferredQuestion.record("Other channel?", slack_channel="D2", slack_ts="600.1", card=OWNER_CARD)
        DeferredQuestionAudit.objects.create(
            question=other_card, action=UNROUTED_AUDIT_ACTION, note="600.2", resolver_id="card_discussion"
        )
        reply = _event(backend, ts="600.2", root="", text="Thanks for the update")
        assert reply is not None

        report = run_slack_answer_cycle(
            messaging_resolver=lambda _overlay: backend,
            reader=lambda _text: InboundReading(
                intent=InboundIntent.FYI, answerable=False, work_summary="", source=ReadingSource.HEURISTIC
            ),
        )

        reply.refresh_from_db()
        assert report.processed == 11
        assert reply.answer_kind == PendingChatInjection.AnswerKind.ACK

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

    def test_root_fast_path_honors_channel_and_reply_time(self) -> None:
        _card("201.1")
        backend = Backend(user_id=OWNER_SLACK_ID)

        assert cards_in_thread(channel="D_OTHER", thread_ts="201.1", before_ts="201.2", backend=backend)[0] == []
        assert cards_in_thread(channel=CHANNEL, thread_ts="201.1", before_ts="201.1", backend=backend)[0] == []
        assert backend.reads == []

    def test_a_later_unrelated_card_does_not_join_a_root_card_thread(self) -> None:
        root = _card("260.1")
        unrelated = _card("260.5")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["260.1"] = [{"ts": "260.1"}, _bot_reminder("260.2", "260.1")]
        reply = _event(backend, ts="260.9", root="260.1")
        assert reply is not None

        _cycle(backend)

        root.refresh_from_db()
        unrelated.refresh_from_db()
        assert root.answer_text == "Approve"
        assert unrelated.is_pending
        assert backend.reads == ["260.1"]

    def test_api_posted_owner_reply_binds_a_root_card_that_holds_bot_reminders(self) -> None:
        decoy = _card("250.1")
        card = _card("300.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        reminders = [_bot_reminder("300.2", "300.1"), _bot_reminder("300.3", "300.1")]
        backend.thread_replies["300.1"] = [{"ts": "300.1"}, *reminders]

        recorded = SlackDmRecorder(backend=backend).record_events([*reminders, _api_owner_reply("300.4", "300.1")])

        assert recorded == 1, "the recorder kept something other than the owner's reply"
        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            report = _cycle(backend)
            card.refresh_from_db()
            decoy.refresh_from_db()
            assert card.resolved_via == DeferredQuestion.ResolvedVia.SLACK
            assert card.answer_text == "Approve", "the bound answer did not use the card's option label"
        assert decoy.is_pending
        assert report.answered_question == 1
        assert not live_owner_questions()
        decoy.refresh_from_db()
        assert decoy.answer_text == "Approve"
        assert decoy.audits.filter(action="coordinated").exists()
        assert backend.reads == []
        assert any("bound question" in line and "key=root" in line for line in logs.output)

    def test_api_user_token_reply_with_api_app_id_binds_nested_card(self) -> None:
        card = _card("330.2")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["330.1"] = [{"ts": "330.1"}, {"ts": "330.2", "thread_ts": "330.1"}]
        event = {**_api_owner_reply("330.3", "330.1"), "api_app_id": "A_OWNER_SCRIPT"}

        recorded = SlackDmRecorder(backend=backend).record_events([event])
        report = _cycle(backend)

        card.refresh_from_db()
        assert recorded == 1
        assert card.answer_text == "Approve"
        assert report.answered_question == 1

    def test_registered_requesters_app_reply_binds_only_its_ticket_card(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        ticket = Ticket.objects.create(requested_by="github:alice")
        card = _card("331.2")
        card.task_session = Session.objects.create(ticket=ticket)
        card.save(update_fields=["task_session"])
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["331.1"] = [{"ts": "331.1"}, {"ts": "331.2", "thread_ts": "331.1"}]
        event = {**_api_owner_reply("331.3", "331.1"), "user": "U-OP"}

        recorded = SlackDmRecorder(backend=backend).record_events([event])
        report = _cycle(backend)

        card.refresh_from_db()
        assert recorded == 1
        assert report.answered_question == 1
        assert card.answer_text == "Approve"

    def test_free_text_without_a_reachable_agent_waits_for_retry_and_leaves_card_pending(self) -> None:
        card = _card("335.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["335.2"] = {"ts": "335.2", "thread_ts": "335.1"}
        reply = _event(backend, ts="335.2", root="335.1", text="Something else")
        assert reply is not None

        _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        assert card.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.UNANSWERED
        assert [text for _channel, _ts, text in backend.replies] == [NOBODY_LISTENING]

    def test_question_outside_card_options_gets_one_thread_note_while_waiting(self) -> None:
        card = _card("336.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["336.2"] = {"ts": "336.2", "thread_ts": "336.1"}
        reply = _event(backend, ts="336.2", root="336.1", text="What does this change?")
        assert reply is not None

        _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        assert card.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.UNANSWERED
        assert reply.loop_response_confirmed_at is None
        assert [text for _channel, _ts, text in backend.replies] == [NOBODY_LISTENING]

        _cycle(backend)

        assert card.audits.filter(action=UNROUTED_AUDIT_ACTION).count() == 1
        assert [text for _channel, _ts, text in backend.replies] == [NOBODY_LISTENING]

    def test_top_level_instruction_with_one_option_card_reaches_answerer(self) -> None:
        card = _card("335.3")
        backend = Backend(user_id=OWNER_SLACK_ID)
        reply = _event(backend, ts="335.4", root="", text="Something else")
        assert reply is not None

        report = _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        assert card.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.DELEGATED
        assert report.dispatched == 1
        assert backend.replies == []

    def test_top_level_question_with_one_option_card_reaches_answerer(self) -> None:
        card = _card("335.5")
        backend = Backend(user_id=OWNER_SLACK_ID)
        reply = _event(backend, ts="335.6", root="", text="What is the status of the build?")
        assert reply is not None

        report = _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        assert card.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.DELEGATED
        assert report.dispatched == 1
        assert backend.replies == []

    def test_scanner_then_cycle_log_one_thread_decision_and_read_once(self) -> None:
        card = _card("335.8")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["335.7"] = [{"ts": "335.7"}, {"ts": "335.8"}]
        reply = _event(backend, ts="335.9", root="335.7", text="Approve")
        assert reply is not None

        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            with patch.object(backend, "get_permalink", return_value=""):
                AskUserQuestionReplyScanner(backend=backend).scan()
            _cycle(backend)

        card.refresh_from_db()
        assert card.answer_text == "Approve"
        assert sum("335.9" in line for line in logs.output) == 1
        assert backend.reads == ["335.7"]

    def test_tap_only_and_option_cards_in_one_thread_bind_the_option(self) -> None:
        action = _action_card("336.12")
        option = _card("336.13")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["336.11"] = [{"ts": "336.11"}, {"ts": "336.12"}, {"ts": "336.13"}]
        reply = _event(backend, ts="336.14", root="336.11", text="Approve")
        assert reply is not None

        _cycle(backend)

        action.refresh_from_db()
        option.refresh_from_db()
        assert action.is_pending
        assert option.answer_text == "Approve"

    def test_second_coalesced_id_reply_selects_its_own_card_and_logs_both_rows(self) -> None:
        first = _card("336.2")
        second = _card("336.3")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["336.1"] = [{"ts": "336.1"}, {"ts": "336.2"}, {"ts": "336.3"}]
        first_reply = _event(backend, ts="336.4", root="336.1", text="approve")
        second_reply = _event(backend, ts="336.5", root="336.1", text=f"#{second.pk} decline")
        assert first_reply is not None
        assert second_reply is not None

        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            _cycle(backend)

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.is_pending
        assert second.answer_text == "Decline"
        assert sum("336.4" in line for line in logs.output) == 1
        assert sum("336.5" in line for line in logs.output) == 1

    def test_each_row_of_an_unsplit_card_thread_turn_has_a_decision_log(self) -> None:
        card = _card("336.6")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["336.7"] = {"ts": "336.7", "thread_ts": "336.6"}
        _event(backend, ts="336.7", root="336.6", text="maybe")
        _event(backend, ts="336.8", root="336.6", text="later")

        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            _cycle(backend)

        card.refresh_from_db()
        assert card.is_pending
        assert sum("336.7" in line for line in logs.output) == 1
        assert sum("336.8" in line for line in logs.output) == 1

    def test_addressed_action_card_discussion_waits_for_a_peer(self) -> None:
        card = _action_card("337.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["337.2"] = {"ts": "337.2", "thread_ts": "337.1"}
        reply = _event(backend, ts="337.2", root="337.1", text=f"#{card.pk} Accept")
        assert reply is not None

        _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        assert card.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.UNANSWERED
        assert [text for _channel, _ts, text in backend.replies] == [NOBODY_LISTENING]

    def test_api_post_by_other_user_in_card_thread_is_not_recorded(self) -> None:
        _card("338.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        event = {**_api_owner_reply("338.2", "338.1"), "user": "U_OTHER"}

        assert SlackDmRecorder(backend=backend).record_events([event]) == 0
        assert not PendingChatInjection.objects.filter(slack_ts="338.2").exists()

    def test_api_bot_post_in_card_thread_is_not_recorded(self) -> None:
        _card("339.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        event = {**_api_owner_reply("339.2", "339.1"), "bot_id": "B_OTHER"}

        assert SlackDmRecorder(backend=backend).record_events([event]) == 0
        assert not PendingChatInjection.objects.filter(slack_ts="339.2").exists()

    def test_own_api_post_in_card_thread_is_not_recorded(self) -> None:
        _card("340.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.auth_user_id = OWNER_SLACK_ID

        assert SlackDmRecorder(backend=backend).record_events([_api_owner_reply("340.2", "340.1")]) == 0
        assert not PendingChatInjection.objects.filter(slack_ts="340.2").exists()

    def test_api_post_in_closed_card_thread_is_not_recorded(self) -> None:
        card = _card("341.1")
        card.apply_answer("Approve", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        backend = Backend(user_id=OWNER_SLACK_ID)

        assert SlackDmRecorder(backend=backend).record_events([_api_owner_reply("341.2", "341.1")]) == 0
        assert not PendingChatInjection.objects.filter(slack_ts="341.2").exists()

    def test_failed_thread_read_does_not_lose_a_direct_message_in_the_batch(self) -> None:
        _card("342.2")
        backend = Backend(user_id=OWNER_SLACK_ID, read_error=True)
        failed = _api_owner_reply("342.3", "342.1")
        direct = {"ts": "342.4", "user": OWNER_SLACK_ID, "channel": CHANNEL, "text": "hello"}
        recorder = SlackDmRecorder(backend=backend)

        with self.assertLogs("teatree.loop.scanners.slack_dm_recorder", level="WARNING"):
            assert recorder.record_events([failed, direct]) == 1

        assert not PendingChatInjection.objects.filter(slack_ts="342.3").exists()
        assert PendingChatInjection.objects.filter(slack_ts="342.4").exists()
        backend.read_error = False
        backend.thread_replies["342.1"] = [{"ts": "342.1"}, {"ts": "342.2"}]
        assert recorder.record_events([failed]) == 1

    def test_api_app_post_without_configured_owner_is_not_recorded(self) -> None:
        card = _card("340.1")
        backend = Backend(user_id="")
        event = {**_api_owner_reply("340.2", "340.1"), "user": "", "api_app_id": "A_OWNER_SCRIPT"}

        recorded = SlackDmRecorder(backend=backend).record_events([event])

        card.refresh_from_db()
        assert recorded == 0
        assert card.is_pending

    def test_api_app_post_in_internal_question_thread_is_not_recorded(self) -> None:
        DeferredQuestion.record("Internal state?", slack_channel=CHANNEL, slack_ts="350.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        event = {**_api_owner_reply("350.2", "350.1"), "api_app_id": "A_OWNER_SCRIPT"}

        recorded = SlackDmRecorder(backend=backend).record_events([event])

        assert recorded == 0

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
        assert len(logs.output) == 2, "each reply is logged once with its decision"

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

    def test_non_owner_reply_is_rejected_without_receipt(self) -> None:
        card = _card("700.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        _event(backend, ts="700.2", root="700.1", user="U_OTHER")

        _cycle(backend)

        card.refresh_from_db()
        assert card.is_pending
        assert backend.reactions == []
        assert backend.replies == []

    def test_api_app_post_in_unrelated_thread_stays_filtered_and_leaves_card_pending(self) -> None:
        card = _card("800.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        raw = {**_api_owner_reply("800.2", "799.1"), "api_app_id": "A_APP"}
        with self.assertLogs("teatree.loop.scanners.slack_dm_recorder", level="INFO") as logs:
            recorded = SlackDmRecorder(backend=backend).record_events([raw])

        report = _cycle(backend)

        card.refresh_from_db()
        assert recorded == 0
        assert report.processed == 0
        assert card.is_pending
        assert not PendingChatInjection.objects.filter(slack_ts="800.2").exists()
        assert "posted through an app token" in " ".join(logs.output)

    def test_action_card_thread_discussion_waits_for_a_peer(self) -> None:
        card = _action_card("850.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["850.2"] = {"ts": "850.2", "thread_ts": "850.1"}
        reply = _event(backend, ts="850.2", root="850.1", text="Accept")
        assert reply is not None

        _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        assert card.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.UNANSWERED
        assert [text for _channel, _ts, text in backend.replies] == [NOBODY_LISTENING]

    def test_reply_naming_a_closed_card_gets_a_note_and_binds_no_other_card(self) -> None:
        pending = _card("700.1")
        closed = _card("700.2")
        closed.apply_answer("Decline", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["700.3"] = {"ts": "700.3", "thread_ts": "700.1"}
        backend.thread_replies["700.1"] = [{"ts": "700.1"}, {"ts": "700.2", "thread_ts": "700.1"}]
        reply = _event(backend, ts="700.3", root="700.1", text=f"#{closed.pk} approve")
        assert reply is not None

        _cycle(backend)

        pending.refresh_from_db()
        reply.refresh_from_db()
        assert pending.is_pending
        assert reply.answer_kind == PendingChatInjection.AnswerKind.ACK
        assert reply.loop_response_confirmed_at is not None
        assert any(f"#{closed.pk}" in text and "no longer" in text.lower() for _, _, text in backend.replies)

    def test_question_text_with_two_pending_cards_is_left_to_the_ordinary_route(self) -> None:
        first = _card("750.2")
        second = _card("750.3")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.thread_replies["750.1"] = [{"ts": "750.1"}, {"ts": "750.2"}, {"ts": "750.3"}]
        reply = _event(backend, ts="750.4", root="750.1", text="What does each option change?")
        assert reply is not None

        with self.assertLogs("teatree.loop.question_binding", level="INFO") as logs:
            _cycle(backend)

        first.refresh_from_db()
        second.refresh_from_db()
        reply.refresh_from_db()
        assert first.is_pending
        assert second.is_pending
        assert reply.answer_kind != PendingChatInjection.AnswerKind.ACK
        assert not any("Start your reply" in text for _, _, text in backend.replies)
        assert "the text reads as a question" in " ".join(logs.output)
        assert "no pending card in this thread" not in " ".join(logs.output)
