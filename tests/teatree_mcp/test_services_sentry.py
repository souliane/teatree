"""Tests for the Sentry read-only MCP tool group (the service-declaration pilot).

The client is resolved from the first registered overlay declaring
``Service.SENTRY`` with a configured org; the HTTP transport is the only mock.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.overlay import OverlayConfig
from teatree.mcp import build_server, services_sentry


class _SentryOverlay:
    def __init__(self, *, org: str = "acme") -> None:
        self.config = OverlayConfig(
            required_third_party_services=frozenset({Service.SENTRY}),
            sentry_org=org,
        )


class TestSentryClientResolution(TestCase):
    def test_resolves_client_from_declaring_overlay(self) -> None:
        with patch("teatree.mcp.services_sentry.get_all_overlays", return_value={"a": _SentryOverlay()}):
            client = services_sentry._client()

        assert client.org == "acme"
        assert client.base_url == "https://sentry.io"

    def test_no_configured_declarer_fails_loud(self) -> None:
        overlays = {"a": _SentryOverlay(org="")}
        with (
            patch("teatree.mcp.services_sentry.get_all_overlays", return_value=overlays),
            pytest.raises(RuntimeError, match="Sentry org"),
        ):
            services_sentry._client()


class TestSentryToolCalls(TestCase):
    def test_top_issues_routes_through_the_backend_client(self) -> None:
        fake = MagicMock()
        fake.get_top_issues.return_value = [{"id": "1", "title": "boom"}]
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SentryOverlay()}):
            server = build_server()
        with patch("teatree.mcp.services_sentry._client", return_value=fake):
            result = async_to_sync(server.call_tool)("sentry_top_issues", {"project": "backend"})

        fake.get_top_issues.assert_called_once_with(project="backend", limit=10)
        assert result

    def test_each_tool_reaches_its_client_read(self) -> None:
        fake = MagicMock()
        fake.get_issue.return_value = {"id": "9"}
        fake.get_issue_events.return_value = [{"eventID": "e1"}]
        fake.list_projects.return_value = [{"slug": "backend"}]
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SentryOverlay()}):
            server = build_server()

        with patch("teatree.mcp.services_sentry._client", return_value=fake):
            async_to_sync(server.call_tool)("sentry_issue_get", {"issue_id": "9"})
            async_to_sync(server.call_tool)("sentry_issue_events", {"issue_id": "9", "limit": 3})
            async_to_sync(server.call_tool)("sentry_projects", {})

        fake.get_issue.assert_called_once_with("9")
        fake.get_issue_events.assert_called_once_with("9", limit=3)
        fake.list_projects.assert_called_once_with()

    def test_registered_sentry_tools_are_read_only(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SentryOverlay()}):
            tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}

        sentry_tools = [tool for name, tool in tools.items() if name.startswith("sentry_")]
        assert len(sentry_tools) == 4
        assert all(tool.annotations and tool.annotations.readOnlyHint for tool in sentry_tools)
