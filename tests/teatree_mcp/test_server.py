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

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Task
from teatree.mcp import build_server
from tests.factories import TaskFactory, TicketFactory

_EXPECTED_TOOLS = {
    "ticket_search",
    "worktree_status",
    "pr_for_ticket",
    "loop_stats",
    "factory_signals",
    "incoming_event_recent",
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
