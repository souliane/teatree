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
from mcp.types import Tool, ToolAnnotations

from teatree.backends.types import Service
from teatree.core.factory.factory_signals import SIGNALS, VISIBILITY_SIGNALS
from teatree.core.modelkit.phase_tools import (
    _MCP_WRITE_TOOLS_BY_PHASE,
    MCP_WRITE,
    mcp_write_tools_for_phase,
    tools_for_phase,
)
from teatree.core.modelkit.phases import KNOWN_PHASES
from teatree.core.models import Task
from teatree.core.overlay import McpTool, McpToolGroup, OverlayConfig, OverlayConnectors
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
    def __init__(self, *services: Service, connectors: OverlayConnectors | None = None) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))
        self.connectors = connectors or OverlayConnectors()


def _overlay_note(subject: str) -> str:
    return subject


class _ContributingConnectors(OverlayConnectors):
    def mcp_tool_group(self) -> McpToolGroup:
        return McpToolGroup(
            tools=(
                McpTool("overlay_peek", _overlay_note, ToolAnnotations(read_only_hint=True)),
                McpTool("overlay_stamp", _overlay_note, ToolAnnotations(read_only_hint=False), seam="demo seam"),
            ),
            instructions="- overlay_peek(subject): read.\n- overlay_stamp(subject): write.",
        )


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


_EVERY_SURFACE_SERVICE = frozenset({Service.GITHUB, Service.GITLAB, Service.SLACK, Service.NOTION})
_MAILBOX_ENV = {"T3_AGENT_MAILBOX_SOCKET": "/tmp/missing.sock", "T3_AGENT_MAILBOX_TOKEN": "test-token"}


def _surface(
    *, read_only: bool, allowed_writes: frozenset[str] = frozenset(), connectors: OverlayConnectors | None = None
) -> tuple[dict[str, Tool], str, dict[str, str]]:
    """Every tool, the instructions and the seamed writes of a server carrying each kind of tool."""
    overlays = {"a": _ServiceOverlay(*_EVERY_SURFACE_SERVICE, connectors=connectors or _ContributingConnectors())}
    with patch.dict("os.environ", _MAILBOX_ENV), patch("teatree.mcp.server.get_all_overlays", return_value=overlays):
        server = build_server(read_only=read_only, allowed_writes=allowed_writes)
        seams = declared_write_tool_seams(_EVERY_SURFACE_SERVICE)
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}, server.instructions or "", seams


class _WriteOnlyConnectors(OverlayConnectors):
    def mcp_tool_group(self) -> McpToolGroup:
        stamp = McpTool("overlay_stamp", _overlay_note, ToolAnnotations(read_only_hint=False), seam="demo seam")
        return McpToolGroup(tools=(stamp,), instructions="- overlay_stamp(subject): write.")


def _is_read_only(tool: Tool) -> bool:
    return bool(tool.annotations and tool.annotations.read_only_hint)


class TestReadOnlySurface(TestCase):
    """A read-only server registers the read half of the full surface, plus the writes it is told to keep."""

    def test_a_read_only_server_registers_no_write_tool(self) -> None:
        tools, _, _ = _surface(read_only=True)
        assert [name for name, tool in tools.items() if not _is_read_only(tool)] == []

    def test_a_read_only_server_keeps_every_read_tool_of_the_full_surface(self) -> None:
        full, _, _ = _surface(read_only=False)
        read_only, _, _ = _surface(read_only=True)
        assert set(read_only) == {name for name, tool in full.items() if _is_read_only(tool)}
        assert {"ticket_get", "github_pr_diff", "overlay_peek", "agent_mailbox_inbox"} <= set(read_only)

    def test_the_withheld_tools_include_every_seamed_write(self) -> None:
        full, _, seams = _surface(read_only=False)
        writes = {name for name, tool in full.items() if not _is_read_only(tool)}
        assert set(seams) <= writes
        assert {"pr_merge", "question_answer", "github_issue_create", "agent_mailbox_send", "overlay_stamp"} <= writes

    def test_the_read_only_instructions_advertise_no_withheld_tool(self) -> None:
        tools, instructions, seams = _surface(read_only=True)
        assert [name for name in seams if f"- {name}(" in instructions] == []
        assert "- review_request_check(" in instructions
        assert "- overlay_peek(" in instructions
        assert "read-only for this dispatch" in instructions
        assert {"review_request_check", "overlay_peek"} <= set(tools)

    def test_a_section_left_with_no_tool_loses_its_header(self) -> None:
        _, read_only, _ = _surface(read_only=True, connectors=_WriteOnlyConnectors())
        _, full, _ = _surface(read_only=False, connectors=_WriteOnlyConnectors())
        assert "Overlay tools (a):" in full
        assert "Overlay tools (a):" not in read_only
        assert "Teatree write tools" in read_only

    def test_the_full_server_keeps_its_writes_and_their_instructions(self) -> None:
        tools, instructions, _ = _surface(read_only=False)
        assert {"pr_merge", "overlay_stamp"} <= set(tools)
        assert "- pr_merge(" in instructions
        assert "read-only for this dispatch" not in instructions


class TestPhaseWriteAllowance(TestCase):
    """Each ``_MCP_WRITE_TOOLS_BY_PHASE`` entry reaches exactly its own write tools on the read-only server."""

    def test_every_entry_registers_its_own_writes_and_no_other(self) -> None:
        for phase, allowed in _MCP_WRITE_TOOLS_BY_PHASE.items():
            with self.subTest(phase=phase):
                assert phase in KNOWN_PHASES
                assert MCP_WRITE not in tools_for_phase(phase)
                tools, instructions, seams = _surface(read_only=True, allowed_writes=mcp_write_tools_for_phase(phase))
                assert {name for name, tool in tools.items() if not _is_read_only(tool)} == allowed
                assert {name for name in seams if f"- {name}(" in instructions} == allowed

    def test_requesting_review_posts_its_request_and_reaches_no_other_write(self) -> None:
        allowed = mcp_write_tools_for_phase("requesting_review")
        tools, instructions, _ = _surface(read_only=True, allowed_writes=allowed)
        assert "review_request_post" in tools
        assert not {"question_answer", "pr_merge", "task_complete"} & set(tools)
        assert "Write tools registered: review_request_post." in instructions
