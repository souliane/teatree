"""A Slack conversation ticket: recognised by its anchor, kept while alive, barred from build phases (#5685)."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.agents.dispatch_refusal import pre_harness_refusal
from teatree.core.models import NoPlanArtifactError, ThreadBinding, Ticket
from teatree.core.models.errors import TaskMintRefusedError
from teatree.core.models.plan_decision import CONVERSATION_REFUSED_PREFIX
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_enqueue import enqueue_phase_task
from teatree.core.models.task_handoff import schedule_resume
from teatree.loop.persistence_phase_task import create_phase_task
from teatree.utils.url_slug import conversation_anchor, slack_conversation_anchor
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

    def test_a_mailbox_anchored_ticket_is_a_conversation(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=conversation_anchor(kind="mailbox", key="ticket/42/agent/a"),
        )

        assert ticket.is_conversation() is True


class TestBuildPhasesAreRefusedOnAConversation(TestCase):
    def test_a_planning_task_can_complete_without_minting_build_work(self) -> None:
        ticket = _conversation()
        task = enqueue_phase_task(ticket=ticket, phase="planning", reason="Plan it.")
        record_test_plan(ticket)

        task.complete()

        task.refresh_from_db()
        ticket.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ticket.state == Ticket.State.NOT_STARTED
        assert not Task.objects.filter(ticket=ticket, phase="coding").exists()

    def test_unplanned_conversation_build_refusal_names_the_conversation(self) -> None:
        ticket = _conversation()

        with pytest.raises(TaskMintRefusedError, match="conversation_ticket: refusing a coding task"):
            enqueue_phase_task(ticket=ticket, phase="coding", reason="build it")

    def test_scheduler_refuses_conversation_build_work(self) -> None:
        ticket = _conversation()

        with pytest.raises(TaskMintRefusedError, match="conversation_ticket: refusing a coding task"):
            ticket.schedule_coding()

        assert not Task.objects.filter(ticket=ticket).exists()

    def test_child_mint_refuses_conversation_build_work(self) -> None:
        ticket = _conversation()
        parent = enqueue_phase_task(ticket=ticket, phase="planning", reason="Plan it.")

        with pytest.raises(TaskMintRefusedError, match="conversation_ticket: refusing a coding task"):
            parent.spawn_child_tasks(["repo"], phase="coding")

        assert not parent.child_tasks.exists()

    def test_resume_refuses_a_preexisting_build_task_on_conversation(self) -> None:
        ticket = _conversation()
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        parent = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)

        with pytest.raises(TaskMintRefusedError, match="conversation_ticket: refusing a coding task"):
            schedule_resume(parent, answer="continue")

        assert not parent.child_tasks.exists()

    def test_loop_mint_refuses_conversation_build_work(self) -> None:
        ticket = _conversation()

        with pytest.raises(TaskMintRefusedError, match="conversation_ticket: refusing a coding task"):
            create_phase_task(ticket, phase="coding", agent_id="coding", reason="build it")

        assert not Task.objects.filter(ticket=ticket).exists()

    def test_an_existing_build_task_cannot_dispatch_on_a_conversation(self) -> None:
        ticket = _conversation()
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        refusal = pre_harness_refusal(task, phase="coding")

        assert refusal is not None
        assert refusal.startswith(CONVERSATION_REFUSED_PREFIX)

    def test_cli_create_reports_the_conversation_refusal(self) -> None:
        ticket = _conversation()
        errors = StringIO()

        with pytest.raises(SystemExit) as raised:
            call_command("tasks", "create", str(ticket.pk), phase="coding", reason="build it", stderr=errors)

        assert raised.value.code == 1
        assert CONVERSATION_REFUSED_PREFIX in errors.getvalue()
        assert not Task.objects.filter(ticket=ticket).exists()

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

    def test_a_mailbox_conversation_refuses_implementation(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=conversation_anchor(kind="mailbox", key="ticket/42/agent/a"),
        )
        record_test_plan(ticket)

        with pytest.raises(TaskMintRefusedError, match="conversation_ticket: refusing a coding task"):
            enqueue_phase_task(ticket=ticket, phase="coding", reason="build it")
