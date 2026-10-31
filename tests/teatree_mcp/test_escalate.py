"""The escalate tool through the real MCP server and a real mailbox broker (#5685)."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase
from mcp.server.mcpserver.exceptions import ToolError

from teatree.agents.live_control import LiveTask
from teatree.agents.live_delivery import MAILBOX_AUTH_ENV, MAILBOX_SOCKET_ENV
from teatree.agents.live_mailbox import LiveMailboxBroker, MailboxIdentity
from teatree.agents.live_registry import LiveSessionRegistry
from teatree.core.answering.escalation import KINDS
from teatree.core.modelkit.phase_tools import mcp_write_tools_for_phase
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.mcp.server import build_server
from tests._escalation_peers import AcceptingSession, live_task_of
from tests.factories import SessionFactory, TaskFactory, TicketFactory
from tests.teatree_agents._live_owner import OwnerLoop
from tests.teatree_mcp._call_tool_result import payloads


def _running(phase: str = "planning") -> Task:
    ticket = TicketFactory(state=Ticket.State.WORK_STARTED)
    return TaskFactory(
        ticket=ticket, session=SessionFactory(ticket=ticket, agent_id=phase), phase=phase, status=Task.Status.CLAIMED
    )


class TestTheEscalateToolOverTheMcpServer(TransactionTestCase):
    def setUp(self) -> None:
        self.registry = LiveSessionRegistry()
        runtime = self.enterContext(tempfile.TemporaryDirectory())
        self.broker = self.enterContext(LiveMailboxBroker(runtime_dir=Path(runtime), registry=lambda: self.registry))
        self.asker = _running()
        self.asker_identity = self._register(self.asker, live_task_of(self.asker))

    def _register(self, task: Task, live: LiveTask) -> MailboxIdentity:
        return self.broker.register(
            room=str(task.ticket_id), harness="claude_sdk", label=f"{task.phase} task {task.pk}", task=live
        )

    def _call(self, args: dict[str, object], *, phase: str = "planning") -> dict[str, object]:
        server = self._server(phase)
        with patch.dict("os.environ", self.asker_identity.mcp_env()):
            return payloads(async_to_sync(server.call_tool)("escalate", args))[0]

    @staticmethod
    def _server(phase: str):
        with patch("teatree.mcp.server.get_all_overlays", return_value={}):
            return build_server(read_only=True, allowed_writes=mcp_write_tools_for_phase(phase))

    def test_a_live_coordinator_is_told_over_the_mailbox_once_the_request_row_exists(self) -> None:
        coordinator = TaskFactory(
            ticket=self.asker.ticket,
            session=SessionFactory(ticket=self.asker.ticket, agent_id="answering"),
            phase="answering",
            status=Task.Status.CLAIMED,
        )
        session = AcceptingSession()
        live = live_task_of(coordinator)
        self._register(coordinator, live)

        with OwnerLoop(self.registry, live, session):
            result = self._call({"kind": "internal", "message": "Run the backfill before the migration?"})

        row = DeferredQuestion.objects.get()
        assert result == {"request_id": row.pk, "delivery": "peer", "deadline_minutes": 30}
        assert len(session.delivered) == 1
        assert f"Escalation {row.pk} from planning task {self.asker.pk}" in session.delivered[0]
        assert "Run the backfill before the migration?" in session.delivered[0]

    def test_without_a_live_coordinator_the_threads_answering_task_is_resumed(self) -> None:
        parent = TaskFactory(
            ticket=self.asker.ticket,
            session=SessionFactory(ticket=self.asker.ticket, agent_id="answering"),
            phase="answering",
            status=Task.Status.COMPLETED,
        )

        result = self._call({"kind": "internal", "message": "Run the backfill before the migration?"})

        row = DeferredQuestion.objects.get()
        assert result["delivery"] == "resumed"
        child = Task.objects.get(parent_task=parent)
        assert f"Escalation {row.pk} from planning task {self.asker.pk}" in child.execution_reason

    def test_a_repeated_identical_call_returns_the_same_request(self) -> None:
        args = {"kind": "internal", "message": "Run the backfill before the migration?"}

        first = self._call(args)
        again = self._call(args)

        assert again["request_id"] == first["request_id"]
        assert DeferredQuestion.objects.count() == 1

    def test_a_refusal_comes_back_as_a_tool_error(self) -> None:
        with pytest.raises(ToolError, match="invalid kind 'spend_money' — valid kinds: internal"):
            self._call({"kind": "spend_money", "message": "Go ahead?"})
        assert not DeferredQuestion.objects.exists()

    def test_a_mailbox_that_is_gone_comes_back_as_a_tool_error(self) -> None:
        server = self._server("planning")
        dead = {MAILBOX_SOCKET_ENV: "/nonexistent/mailbox.sock", MAILBOX_AUTH_ENV: "token"}
        with patch.dict("os.environ", dead), pytest.raises(ToolError, match="Live mailbox is unavailable"):
            async_to_sync(server.call_tool)("escalate", {"kind": "internal", "message": "Help?"})
        assert not DeferredQuestion.objects.exists()

    def test_a_server_with_no_mailbox_identity_refuses(self) -> None:
        server = self._server("planning")
        with (
            patch.dict("os.environ", {MAILBOX_SOCKET_ENV: "", MAILBOX_AUTH_ENV: ""}),
            pytest.raises(ToolError, match="bound to a live mailbox"),
        ):
            async_to_sync(server.call_tool)("escalate", {"kind": "internal", "message": "Help?"})


class TestWhichServersOfferEscalate(TransactionTestCase):
    @staticmethod
    def _names(phase: str, *, full: bool = False) -> set[str]:
        with patch("teatree.mcp.server.get_all_overlays", return_value={}):
            server = (
                build_server()
                if full
                else build_server(read_only=True, allowed_writes=mcp_write_tools_for_phase(phase))
            )
        return {tool.name for tool in asyncio.run(server.list_tools())}

    def test_the_phases_that_hold_the_grant_list_the_tool(self) -> None:
        for phase in ("planning", "reviewing", "critic_reviewing"):
            assert "escalate" in self._names(phase), phase
        assert "escalate" in self._names("coding", full=True)

    def test_the_description_names_every_kind_the_request_accepts(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={}):
            tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}

        assert all(kind in (tools["escalate"].description or "") for kind in KINDS)

    def test_the_answering_phase_never_lists_the_tool(self) -> None:
        assert "escalate" not in self._names("answering")
        assert "escalate" not in self._names("requesting_review")
