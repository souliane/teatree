"""Tests for the MCPServer server wiring.

Registration is asserted on the live tool metadata; the call path is exercised
end to end through ``MCPServer.call_tool`` against the test DB. ``async_to_sync``
drives the async tool so the ``thread_sensitive`` ORM access runs on the test's
own thread and connection — the factory rows are visible under the normal
transactional ``django_db`` fixture, no committed-transaction dance needed.
"""

import asyncio
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.factory.factory_signals import SIGNALS, VISIBILITY_SIGNALS
from teatree.core.models import Task
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp.server import _required_services, build_server, declared_write_tool_seams
from tests.factories import TaskFactory, TicketFactory
from tests.teatree_mcp._call_tool_result import payloads as _payloads

_READ_TOOLS = {
    "ticket_search",
    "ticket_get",
    "ticket_list",
    "worktree_status",
    "pr_for_ticket",
    "loop_stats",
    "task_list",
    "question_list",
    "factory_signals",
    "factory_score",
    "incoming_event_recent",
    "config_setting_get",
    "gate_status",
    "command_search",
    "review_request_check",
}

# The teatree-own write tools are always registered (they are not service-gated).
_WRITE_TOOLS = {
    "pr_create",
    "pr_merge",
    "ticket_visit_phase",
    "record_e2e_run",
    "config_setting_set",
    "task_create",
    "task_complete",
    "task_fail",
    "notify_user",
    "question_answer",
    "worktree_teardown",
    "review_post_comment",
    "review_post_comments",
    "review_request_post",
}

_EXPECTED_TOOLS = _READ_TOOLS | _WRITE_TOOLS
_MAILBOX_TOOLS = {
    "agent_mailbox_self",
    "agent_mailbox_peers",
    "agent_mailbox_send",
    "agent_mailbox_inbox",
    "agent_mailbox_wait",
}


class TestToolRegistration(TestCase):
    def test_registers_the_expected_tool_surface_with_correct_read_write_hints(self) -> None:
        # No service declared ⇒ the base surface is exactly the read + write tools.
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay()}):
            tools = asyncio.run(build_server().list_tools())

        by_name = {tool.name: tool for tool in tools}
        assert set(by_name) == _EXPECTED_TOOLS
        assert all(by_name[name].annotations and by_name[name].annotations.read_only_hint for name in _READ_TOOLS)
        assert all(not by_name[name].annotations.read_only_hint for name in _WRITE_TOOLS)

    def test_mailbox_tools_exist_only_for_a_runner_bound_mcp_child(self) -> None:
        env = {"T3_AGENT_MAILBOX_SOCKET": "/tmp/missing.sock", "T3_AGENT_MAILBOX_TOKEN": "test-token"}
        with patch.dict("os.environ", env), patch("teatree.mcp.server.get_all_overlays", return_value={}):
            bound = build_server()
            names = {tool.name for tool in asyncio.run(bound.list_tools())}
            assert "agent_mailbox_send" in declared_write_tool_seams(frozenset())
        assert names & _MAILBOX_TOOLS == _MAILBOX_TOOLS
        assert "agent_mailbox_send" in (bound.instructions or "")

    def test_ticket_search_advertises_its_filter_parameters(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}

        properties = set(tools["ticket_search"].input_schema["properties"])

        assert {"overlay", "state", "kind", "role", "text", "in_flight", "limit"} <= properties

    def test_instructions_name_every_read_tool_the_table_registers(self) -> None:
        # The descriptor table is the single source for both registration and the
        # instruction prose: a read tool added to the table without an instruction
        # (or vice versa) must fail here.
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay()}):
            server = build_server()
        instructions = server.instructions or ""
        registered = {tool.name for tool in asyncio.run(server.list_tools())}

        for name in _READ_TOOLS & registered:
            assert f"- {name}(" in instructions, f"read tool {name} registered but not named in instructions"


class TestCallToolThroughServer(TestCase):
    def test_ticket_search_returns_real_rows(self) -> None:
        ticket = TicketFactory(overlay="t3-teatree", issue_url="https://x/issues/123", short_description="serve me")
        server = build_server()

        result = async_to_sync(server.call_tool)("ticket_search", {"overlay": "t3-teatree", "text": "serve"})

        ids = {payload["id"] for payload in _payloads(result)}
        assert ticket.pk in ids

    def test_loop_stats_returns_task_counts(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        server = build_server()

        result = async_to_sync(server.call_tool)("loop_stats", {})

        stats = _payloads(result)[0]
        assert stats["tasks"]["pending"] >= 1

    def test_factory_signals_returns_every_registered_signal(self) -> None:
        server = build_server()

        result = async_to_sync(server.call_tool)("factory_signals", {})

        report = _payloads(result)[0]
        assert len(report["signals"]) == len(SIGNALS) + len(VISIBILITY_SIGNALS)
        assert report["verdict"] in {"ok", "regressing", "red"}

    def test_unknown_ticket_reference_returns_empty(self) -> None:
        server = build_server()

        result = async_to_sync(server.call_tool)("worktree_status", {"ticket": "999999"})

        assert _payloads(result) == []

    def test_ticket_list_returns_real_rows(self) -> None:
        ticket = TicketFactory(overlay="t3-teatree", state="coded", issue_url="https://x/issues/700")
        server = build_server()

        result = async_to_sync(server.call_tool)("ticket_list", {"state": "coded"})

        assert ticket.pk in {payload["id"] for payload in _payloads(result)}

    def test_ticket_get_returns_a_single_detail_object(self) -> None:
        ticket = TicketFactory(issue_url="https://x/issues/701")
        server = build_server()

        result = async_to_sync(server.call_tool)("ticket_get", {"ticket": str(ticket.pk)})

        payload = _payloads(result)[0]
        assert payload["id"] == ticket.pk
        assert "visited_phases" in payload

    def test_config_setting_get_reports_the_source(self) -> None:
        server = build_server()

        result = async_to_sync(server.call_tool)("config_setting_get", {"key": "approved_recipe_sha"})

        payload = _payloads(result)[0]
        assert payload["source"] in {"db", "file/env"}

    def test_task_list_returns_real_rows(self) -> None:
        task = TaskFactory(status=Task.Status.PENDING)
        server = build_server()

        result = async_to_sync(server.call_tool)("task_list", {"status": "pending"})

        assert task.pk in {payload["id"] for payload in _payloads(result)}

    def test_gate_status_reports_the_review_gate(self) -> None:
        server = build_server()

        result = async_to_sync(server.call_tool)("gate_status", {})

        report = _payloads(result)[0]
        assert "require_human_approval_to_merge" in report["review_gate"]
        assert "out_of_band_merge_gate_enabled" in report["raw_merge_gate"]

    def test_command_search_finds_a_real_command(self) -> None:
        import teatree.cli  # noqa: F401, PLC0415 — registers the live command-catalogue provider

        server = build_server()

        result = async_to_sync(server.call_tool)("command_search", {"query": "mcp serve"})

        assert any(payload["path"] == "t3 mcp serve" for payload in _payloads(result))


class _ServiceOverlay:
    def __init__(self, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))
        self.connectors = OverlayConnectors()


_SENTRY_TOOLS = {"sentry_top_issues", "sentry_issue_get", "sentry_issue_events", "sentry_projects"}


class TestServiceDeclarationGating(TestCase):
    def test_union_spans_all_registered_overlays(self) -> None:
        overlays = {"a": _ServiceOverlay(Service.GITHUB), "b": _ServiceOverlay(Service.SENTRY, Service.SLACK)}
        with patch("teatree.mcp.server.get_all_overlays", return_value=overlays):
            assert _required_services() == frozenset({Service.GITHUB, Service.SENTRY, Service.SLACK})

    def test_no_overlays_means_no_services(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={}):
            assert _required_services() == frozenset()

    def test_no_declaration_registers_zero_service_tools(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay()}):
            names = {tool.name for tool in asyncio.run(build_server().list_tools())}

        assert names == _EXPECTED_TOOLS

    def test_sentry_tools_registered_iff_sentry_declared(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay(Service.SENTRY)}):
            names = {tool.name for tool in asyncio.run(build_server().list_tools())}

        assert names >= _SENTRY_TOOLS

    def test_other_declaration_does_not_register_sentry_tools(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay(Service.GITHUB)}):
            names = {tool.name for tool in asyncio.run(build_server().list_tools())}

        assert not (_SENTRY_TOOLS & names)

    def test_instructions_advertise_only_registered_groups(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay(Service.SENTRY)}):
            declared = build_server().instructions
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay()}):
            undeclared = build_server().instructions

        assert declared is not None
        assert undeclared is not None
        assert "sentry_top_issues" in declared
        assert "sentry" not in undeclared
