"""A Slack conversation ticket: recognised by its anchor, kept while alive, barred from build phases (#5685)."""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import NoPlanArtifactError, ThreadBinding, Ticket
from teatree.core.models.conversation_ticket import CONVERSATION_QUIET_LIMIT, CONVERSATION_REFUSED_PREFIX
from teatree.core.models.errors import TaskMintRefusedError
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_enqueue import enqueue_phase_task
from teatree.utils.url_slug import slack_conversation_anchor
from tests.factories import record_test_plan

_FORGE_ISSUE = "https://github.com/souliane/teatree/issues/4084"


def _conversation(*, ts: str = "1700000000.000100") -> Ticket:
    return Ticket.objects.create(
        overlay="t3-teatree",
        issue_url=slack_conversation_anchor(channel="D1", slack_ts=ts),
        short_description="Is the rounding still wrong?",
    )


class TestIsConversation(TestCase):
    def test_a_slack_anchored_ticket_is_a_conversation_and_a_forge_issue_is_not(self) -> None:
        conversation = _conversation()
        forge = Ticket.objects.create(overlay="t3-teatree", issue_url=_FORGE_ISSUE, short_description="Fix rounding")

        assert (conversation.is_conversation(), forge.is_conversation()) == (True, False)

    def test_a_ticket_with_no_anchor_is_not_a_conversation(self) -> None:
        assert Ticket.objects.create(overlay="t3-teatree").is_conversation() is False


class TestConversationInUse(TestCase):
    def test_a_live_task_keeps_the_conversation_alive_whatever_the_binding_age(self) -> None:
        ticket = _conversation()
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
        Task.objects.create(ticket=ticket, session=session, phase="answering")

        assert ticket.conversation_in_use() is True

    def test_a_young_binding_keeps_the_conversation_alive(self) -> None:
        ticket = _conversation()
        ThreadBinding.bind(ticket=ticket, channel="D1", thread_ts="1700000000.000100")

        assert ticket.conversation_in_use() is True

    def test_a_binding_older_than_the_quiet_limit_with_no_task_lets_it_go(self) -> None:
        ticket = _conversation()
        ThreadBinding.bind(ticket=ticket, channel="D1", thread_ts="1700000000.000100")
        ThreadBinding.objects.update(created_at=timezone.now() - CONVERSATION_QUIET_LIMIT - timedelta(minutes=1))

        assert ticket.conversation_in_use() is False

    def test_a_finished_task_inside_the_quiet_limit_keeps_it_alive_for_the_next_reply(self) -> None:
        ticket = _conversation()
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
        Task.objects.create(ticket=ticket, session=session, phase="answering", status=Task.Status.COMPLETED)

        assert ticket.conversation_in_use() is True

    def test_no_task_and_no_binding_means_nothing_holds_it(self) -> None:
        assert _conversation().conversation_in_use() is False

    def test_a_forge_issue_ticket_is_never_a_live_conversation(self) -> None:
        forge = Ticket.objects.create(overlay="t3-teatree", issue_url=_FORGE_ISSUE, short_description="Fix rounding")
        ThreadBinding.bind(ticket=forge, channel="D1", thread_ts="1700000000.000100")

        assert forge.conversation_in_use() is False


class TestBuildPhasesAreRefusedOnAConversation(TestCase):
    def test_coding_testing_and_shipping_are_refused_with_the_typed_reason(self) -> None:
        ticket = _conversation()
        record_test_plan(ticket)

        for phase in ("coding", "testing", "shipping"):
            with self.subTest(phase):
                with pytest.raises(TaskMintRefusedError) as raised:
                    enqueue_phase_task(ticket=ticket, phase=phase, reason="build it")
                assert str(raised.value).startswith(CONVERSATION_REFUSED_PREFIX)

        assert Task.objects.filter(ticket=ticket).count() == 0

    def test_the_refusal_is_not_a_missing_plan(self) -> None:
        ticket = _conversation()
        record_test_plan(ticket)

        with pytest.raises(TaskMintRefusedError) as raised:
            enqueue_phase_task(ticket=ticket, phase="coding", reason="build it")

        assert not isinstance(raised.value, NoPlanArtifactError)

    def test_planning_and_answering_are_still_allowed(self) -> None:
        ticket = _conversation()

        planning = enqueue_phase_task(ticket=ticket, phase="planning", reason="Plan it.")
        answering = enqueue_phase_task(ticket=ticket, phase="answering", reason="Answer it.")

        assert (planning.phase, answering.phase) == ("planning", "answering")

    def test_the_promoted_forge_ticket_codes_once_planned(self) -> None:
        conversation = _conversation()
        work = Ticket.objects.create(overlay="t3-teatree", issue_url=_FORGE_ISSUE, short_description="Fix rounding")
        binding = ThreadBinding.bind(ticket=conversation, channel="D1", thread_ts="1700000000.000100")
        ThreadBinding.objects.filter(pk=binding.pk).update(ticket=work)
        record_test_plan(work)

        task = enqueue_phase_task(ticket=work, phase="coding", reason="Fix it.")

        assert task.phase == "coding"
