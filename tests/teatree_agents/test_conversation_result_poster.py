"""A planning task queued on a conversation posts its plan once into the bound thread (#5685)."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.conversation_result_poster import SUMMARY_LIMIT, post_conversation_plan
from teatree.core.models import (
    BotPing,
    NoCurrentPlanError,
    OperatorIdentity,
    Session,
    Task,
    TaskAttempt,
    ThreadBinding,
    Ticket,
)
from teatree.core.notify_types import NotifyReason
from teatree.utils.url_slug import slack_conversation_anchor

_CHANNEL = "D-owner"
_ROOT_TS = "1700000000.000100"
_OWNER_SENTENCE = "Please make the nightly digest quieter on weekends."
_PLAN = (
    "# Quieter weekend digest\n\n"
    "Skip the digest DM on Saturday and Sunday unless something is red.\n\n"
    "## Steps\n\n"
    "Detail that belongs to the stored plan only."
)
_BUILD_HINT = "reply in this thread"


def _adequacy() -> dict:
    return {
        "design": {"content": "skip the weekend digest"},
        "integration_seams": {"content": ["src/teatree/loop/digest.py"]},
        "edge_cases": {"none_reason": "no edges: a weekday digest is untouched"},
        "test_strategy": {"content": "record a planning envelope and assert the post"},
        "acceptance_criteria": {"content": ["the weekend digest is not sent unless red"]},
    }


def _envelope(plan_text: str = _PLAN, **overrides: object) -> dict:
    return {"plan_text": plan_text, "base_sha": "a" * 40, "adequacy": _adequacy(), **overrides}


def _claimed_task(ticket: Ticket, *, phase: str = "planning") -> Task:
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(ticket=ticket, session=session, phase=phase)
    task.claim(claimed_by="loop-slot")
    return task


def _conversation(*, bound: bool = True, requested_by: str = "") -> Ticket:
    ticket = Ticket.objects.create(
        issue_url=slack_conversation_anchor(channel=_CHANNEL, slack_ts=_ROOT_TS),
        overlay="acme",
        role=Ticket.Role.AUTHOR,
        short_description=_OWNER_SENTENCE,
        requested_by=requested_by,
        extra={"slack_answer": {"channel": _CHANNEL, "slack_ts": _ROOT_TS, "question": _OWNER_SENTENCE}},
    )
    if bound:
        ThreadBinding.bind(ticket=ticket, channel=_CHANNEL, thread_ts=_ROOT_TS)
    return ticket


class _PostingTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.backend = mock.MagicMock()
        self.backend.open_dm.return_value = _CHANNEL
        self.backend.post_message.return_value = {"ok": True, "ts": "1700000000.000200"}
        self.backend.get_permalink.return_value = ""
        for patch in (
            mock.patch("teatree.core.notify.resolve_owner_dm_backend", return_value=(self.backend, NotifyReason.NONE)),
            mock.patch("teatree.core.notify.resolve_user_id", return_value="U_OWNER"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def posted_texts(self) -> list[str]:
        return [call.kwargs["text"] for call in self.backend.post_message.call_args_list]


class TestAPlanningTaskOnAConversationPostsItsPlan(_PostingTestCase):
    def test_posts_one_message_into_the_bound_thread(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(), phase="planning")

        assert task.status == Task.Status.COMPLETED
        self.backend.post_message.assert_called_once()
        assert self.backend.post_message.call_args.kwargs["channel"] == _CHANNEL
        assert self.backend.post_message.call_args.kwargs["thread_ts"] == _ROOT_TS

    def test_the_message_carries_the_title_and_first_paragraph_only(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(), phase="planning")

        (text,) = self.posted_texts()
        assert "Quieter weekend digest" in text
        assert "Skip the digest DM on Saturday and Sunday unless something is red." in text
        assert "#" not in text
        assert "Detail that belongs to the stored plan only." not in text

    def test_the_message_says_how_to_ask_for_the_build(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(), phase="planning")

        assert _BUILD_HINT in self.posted_texts()[0]

    def test_a_long_first_paragraph_is_trimmed(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(plan_text="word " * 400), phase="planning")

        (text,) = self.posted_texts()
        assert "word…" in text
        assert len(text) < 1000

    def test_a_first_paragraph_at_the_limit_is_not_trimmed(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(plan_text="a" * SUMMARY_LIMIT), phase="planning")

        assert "…" not in self.posted_texts()[0]

    def test_none_of_the_owner_message_is_posted(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(), phase="planning")

        assert "nightly digest" not in self.posted_texts()[0]

    def test_a_plan_without_a_heading_is_posted_as_its_first_paragraph(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(plan_text="Opening paragraph.\n\nSecond paragraph."), phase="planning")

        (text,) = self.posted_texts()
        assert "Opening paragraph." in text
        assert "Second paragraph." not in text

    def test_a_plan_without_a_blank_line_is_posted_whole(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(plan_text="Single paragraph plan."), phase="planning")

        assert "Single paragraph plan." in self.posted_texts()[0]

    def test_a_heading_only_plan_is_posted_without_its_marker(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(plan_text="## Only a title"), phase="planning")

        (text,) = self.posted_texts()
        assert "Only a title" in text
        assert "#" not in text

    def test_the_post_is_an_owner_delivery_answer(self) -> None:
        task = _claimed_task(_conversation())

        record_result_envelope(task, _envelope(), phase="planning")

        ping = BotPing.objects.get(idempotency_key=f"conversation-plan:{task.pk}")
        assert (ping.kind, ping.audience) == (BotPing.Kind.ANSWER, "owner_delivery")

    def test_posting_the_same_task_again_sends_nothing_twice(self) -> None:
        task = _claimed_task(_conversation())
        record_result_envelope(task, _envelope(), phase="planning")

        post_conversation_plan(task, _envelope(), phase="planning")

        self.backend.post_message.assert_called_once()

    def test_a_re_recorded_envelope_sends_nothing_twice(self) -> None:
        task = _claimed_task(_conversation())
        record_result_envelope(task, _envelope(), phase="planning")
        Task.objects.filter(pk=task.pk).update(status=Task.Status.CLAIMED)
        task.refresh_from_db()

        record_result_envelope(task, _envelope(), phase="planning")

        self.backend.post_message.assert_called_once()

    def test_the_oldest_bound_thread_receives_the_plan(self) -> None:
        conversation = _conversation()
        ThreadBinding.bind(ticket=conversation, channel=_CHANNEL, thread_ts="1700000999.000300")
        task = _claimed_task(conversation)

        record_result_envelope(task, _envelope(), phase="planning")

        assert self.backend.post_message.call_args.kwargs["thread_ts"] == _ROOT_TS

    def test_each_planning_task_on_one_conversation_posts_its_own_plan(self) -> None:
        conversation = _conversation()
        first = _claimed_task(conversation)
        record_result_envelope(first, _envelope(), phase="planning")
        second = _claimed_task(conversation)

        record_result_envelope(second, _envelope(plan_text="A second, revised plan."), phase="planning")

        assert len(self.posted_texts()) == 2
        assert "A second, revised plan." in self.posted_texts()[1]

    def test_a_registered_requester_gets_the_plan_in_their_own_direct_message(self) -> None:
        user = get_user_model().objects.create_superuser(username="colleague", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        task = _claimed_task(_conversation(requested_by="slack:U-OP"))

        record_result_envelope(task, _envelope(), phase="planning")

        self.backend.open_dm.assert_called_once_with("U-OP")

    def test_a_failed_post_does_not_fail_the_recording(self) -> None:
        task = _claimed_task(_conversation())

        with (
            mock.patch("teatree.agents.conversation_result_poster.notify_user_outcome", side_effect=RuntimeError),
            self.assertLogs("teatree.agents.conversation_result_poster", level="WARNING"),
        ):
            attempt = record_result_envelope(task, _envelope(), phase="planning")

        assert attempt.error == ""
        assert task.status == Task.Status.COMPLETED


class TestNothingIsPostedWhenThePlanIsNotTheConversationsToShow(_PostingTestCase):
    def test_a_conversation_with_no_bound_thread_posts_nothing(self) -> None:
        task = _claimed_task(_conversation(bound=False))

        with self.assertNoLogs("teatree.agents.conversation_result_poster", level="WARNING"):
            record_result_envelope(task, _envelope(), phase="planning")

        assert task.status == Task.Status.COMPLETED
        self.backend.post_message.assert_not_called()

    def test_a_forge_ticket_plan_posts_nothing_even_when_a_thread_is_bound(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://github.com/acme/app/issues/7",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.WORK_STARTED,
            overlay="acme",
        )
        ThreadBinding.bind(ticket=ticket, channel=_CHANNEL, thread_ts=_ROOT_TS)
        task = _claimed_task(ticket)

        record_result_envelope(task, _envelope(), phase="planning")

        assert task.status == Task.Status.COMPLETED
        self.backend.post_message.assert_not_called()

    def test_an_answering_task_posts_no_plan(self) -> None:
        task = _claimed_task(_conversation(), phase="answering")

        post_conversation_plan(task, _envelope(), phase="answering")

        self.backend.post_message.assert_not_called()

    def test_a_result_without_plan_text_posts_nothing(self) -> None:
        task = _claimed_task(_conversation())

        for result in ({}, {"plan_text": ""}, {"plan_text": "  \n "}, {"plan_text": 5}):
            with self.subTest(result=result):
                post_conversation_plan(task, result, phase="planning")

        self.backend.post_message.assert_not_called()

    def test_a_refused_plan_posts_nothing(self) -> None:
        task = _claimed_task(_conversation())

        attempt = record_result_envelope(task, _envelope(base_sha=""), phase="planning")

        assert attempt.error
        self.backend.post_message.assert_not_called()

    def test_a_task_that_fails_after_the_plan_is_recorded_posts_nothing(self) -> None:
        task = _claimed_task(_conversation())

        with mock.patch.object(Task, "complete", side_effect=NoCurrentPlanError("stale")):
            attempt = record_result_envelope(task, _envelope(), phase="planning")

        assert attempt.error
        assert TaskAttempt.objects.filter(task=task, error="").count() == 0
        self.backend.post_message.assert_not_called()
