"""Tests for the FastMCP server wiring.

Registration is asserted on the live tool metadata; the call path is exercised
end to end through ``FastMCP.call_tool`` against the test DB. ``async_to_sync``
drives the async tool so the ``thread_sensitive`` ORM access runs on the test's
own thread and connection — the factory rows are visible under the normal
transactional ``django_db`` fixture, no committed-transaction dance needed.
"""

import asyncio
import json
from typing import Any
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.models import Task
from teatree.core.overlay import OverlayConfig
from teatree.mcp import build_server
from teatree.mcp.server import _required_services
from tests.factories import TaskFactory, TicketFactory

_EXPECTED_TOOLS = {
    "ticket_search",
    "ticket_get",
    "ticket_list",
    "worktree_status",
    "pr_for_ticket",
    "loop_stats",
    "task_list",
    "factory_signals",
    "incoming_event_recent",
    "config_setting_get",
    "gate_status",
    "command_search",
}


def _payloads(result: Any) -> list[Any]:
    """Decode the JSON carried in a call_tool result's content blocks."""
    blocks = result[0] if isinstance(result, tuple) else result
    decoded: list[Any] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            decoded.append(json.loads(text))
    return decoded


class TestToolRegistration(TestCase):
    def test_registers_exactly_the_read_only_tool_surface(self) -> None:
        tools = asyncio.run(build_server().list_tools())

        assert {tool.name for tool in tools} == _EXPECTED_TOOLS
        assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools)

    def test_ticket_search_advertises_its_filter_parameters(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}

        properties = set(tools["ticket_search"].inputSchema["properties"])

        assert {"overlay", "state", "kind", "role", "text", "in_flight", "limit"} <= properties


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

    def test_factory_signals_returns_five_signals(self) -> None:
        server = build_server()

        result = async_to_sync(server.call_tool)("factory_signals", {})

        report = _payloads(result)[0]
        assert len(report["signals"]) == 5
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

        result = async_to_sync(server.call_tool)("config_setting_get", {"key": "factory_score_enabled"})

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


class TestFactoryScoreFlagGating(TestCase):
    def test_factory_score_absent_when_flag_off(self) -> None:
        # The shipped OFF state: the outer loop has no MCP metric-to-beat surface.
        names = {tool.name for tool in asyncio.run(build_server().list_tools())}
        assert "factory_score" not in names

    def test_factory_score_registered_when_flag_on(self) -> None:
        call_command("config_setting", "set", "factory_score_enabled", "true")
        names = {tool.name for tool in asyncio.run(build_server().list_tools())}
        assert "factory_score" in names

    def test_factory_score_returns_a_score_payload_when_on(self) -> None:
        call_command("config_setting", "set", "factory_score_enabled", "true")
        server = build_server()

        result = async_to_sync(server.call_tool)("factory_score", {})

        payload = _payloads(result)[0]
        assert payload["verdict"] in {"ok", "regressing", "red"}
        assert "recipe_sha" in payload
        assert len(payload["signals"]) == 5
