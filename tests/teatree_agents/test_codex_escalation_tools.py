"""The Codex lane's escalate tool: offered by phase grant, answered through the shared escalation (#5685)."""

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TransactionTestCase

from teatree.agents import codex_app_server_options
from teatree.agents.codex_app_server_events import dynamic_mailbox_tools, mailbox_tool_result
from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.codex_escalation_tools import ESCALATE_SPEC
from teatree.agents.live_control import LiveTask
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.core.answering.escalation import KINDS
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from tests.factories import SessionFactory, TaskFactory, TicketFactory


def _tool_names(phase: str | None) -> set[str]:
    return {tool["name"] for tool in dynamic_mailbox_tools(phase)}


class TestWhichCodexPhasesAreOfferedEscalate(TransactionTestCase):
    def test_the_granted_phases_are_offered_it_and_the_answering_phase_is_not(self) -> None:
        for phase in ("planning", "reviewing", "critic_reviewing", "coding", "shipping"):
            assert "escalate" in _tool_names(phase), phase
        for phase in ("answering", "requesting_review", None):
            assert "escalate" not in _tool_names(phase), phase

    def test_the_schema_requires_both_arguments_and_names_every_kind(self) -> None:
        (tool,) = (item for item in dynamic_mailbox_tools("planning") if item["name"] == "escalate")

        assert tool["inputSchema"]["required"] == ["kind", "message"]
        assert tool["inputSchema"]["properties"]["kind"]["enum"] == list(KINDS)
        assert tool["inputSchema"]["additionalProperties"] is False
        assert ESCALATE_SPEC[2]["kind"]["enum"] == list(KINDS)


class TestACodexTaskEscalates(TransactionTestCase):
    def setUp(self) -> None:
        self.ticket = TicketFactory(state=Ticket.State.WORK_STARTED)
        self.asker = TaskFactory(
            ticket=self.ticket,
            session=SessionFactory(ticket=self.ticket),
            phase="planning",
            status=Task.Status.CLAIMED,
        )
        self.parent = TaskFactory(
            ticket=self.ticket,
            session=SessionFactory(ticket=self.ticket),
            phase="answering",
            status=Task.Status.COMPLETED,
        )
        runtime = self.enterContext(tempfile.TemporaryDirectory())
        broker = self.enterContext(LiveMailboxBroker(runtime_dir=Path(runtime)))
        live = LiveTask(
            pk=self.asker.pk, ticket=self.ticket.pk, phase="planning", harness="codex_app_server", model="m"
        )
        identity = broker.register(
            room=str(self.ticket.pk), harness="codex_app_server", label=f"planning task {self.asker.pk}", task=live
        )
        self.mailbox = MailboxClient(identity.socket_path, identity.token)

    def _options(self, phase: str) -> CodexAppServerOptions:
        with patch.object(codex_app_server_options, "container_is_the_sandbox", return_value=True):
            options = CodexAppServerOptions.from_sdk_options(
                ClaudeAgentOptions(cwd="/work", permission_mode="bypassPermissions")
            )
        return replace(options, core=replace(options.core, mailbox=self.mailbox), mailbox_phase=phase)

    def _call(self, phase: str, arguments: dict[str, object]) -> dict[str, object] | None:
        request = {"threadId": "thread-1", "tool": "escalate", "arguments": arguments}
        return async_to_sync(mailbox_tool_result)(request, {"thread-1": self._options(phase)})

    def test_it_writes_one_request_and_resumes_the_threads_coordinator(self) -> None:
        result = self._call("planning", {"kind": "internal", "message": "Backfill before the migration?"})

        row = DeferredQuestion.objects.get()
        assert result is not None
        assert result["success"] is True
        assert json.loads(result["contentItems"][0]["text"]) == {
            "request_id": row.pk,
            "delivery": "resumed",
            "deadline_minutes": 30,
        }
        assert Task.objects.get(parent_task=self.parent).phase == "answering"

    def test_a_refusal_is_a_failed_tool_result_that_names_what_to_change(self) -> None:
        result = self._call("planning", {"kind": "spend_money", "message": "Buy more capacity?"})

        assert result is not None
        assert result["success"] is False
        assert "valid kinds" in result["contentItems"][0]["text"]
        assert not DeferredQuestion.objects.exists()

    def test_the_answering_phase_cannot_reach_it(self) -> None:
        assert self._call("answering", {"kind": "internal", "message": "Help?"}) is None
        assert not DeferredQuestion.objects.exists()
