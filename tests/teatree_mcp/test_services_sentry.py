"""Tests for the Sentry read-only MCP tool group (the service-declaration pilot).

The client is resolved from the first registered overlay declaring
``Service.SENTRY`` with a configured org through the
``sentry_client_from_overlay`` core seam (no direct ``teatree.backends.sentry``
import in ``teatree.mcp``); the HTTP transport is the only mock.
"""

import asyncio
import contextlib
import dataclasses
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.types import Service
from teatree.core.backend_protocols import PrMergeState, PrMessage
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp import services_sentry
from teatree.mcp.server import build_server
from tests.teatree_mcp._call_tool_result import structured


@contextlib.contextmanager
def _builder(**mock_kwargs: object) -> Iterator[MagicMock]:
    build = MagicMock(**mock_kwargs)
    with patch.object(services_sentry, "SENTRY", dataclasses.replace(services_sentry.SENTRY, build=build)):
        yield build


class _SentryOverlay:
    def __init__(self, *, org: str = "acme") -> None:
        self.config = OverlayConfig(
            required_third_party_services=frozenset({Service.SENTRY}),
            sentry_org=org,
        )
        self.connectors = OverlayConnectors()


class TestSentryClientResolution(TestCase):
    def test_resolves_client_from_declaring_overlay(self) -> None:
        built = MagicMock(org="acme", base_url="https://sentry.io")
        with (
            patch("teatree.mcp.service_resolver.get_all_overlays", return_value={"a": _SentryOverlay()}),
            _builder(return_value=built) as build,
        ):
            client = services_sentry._client()

        assert client is built
        build.assert_called_once_with("a")

    def test_no_configured_declarer_fails_loud(self) -> None:
        # The overlay declares Sentry, but its factory build yields no client
        # (no sentry_org) — the resolver falls through to the loud refusal.
        with (
            patch("teatree.mcp.service_resolver.get_all_overlays", return_value={"a": _SentryOverlay(org="")}),
            _builder(return_value=None),
            pytest.raises(ToolError, match="Sentry org"),
        ):
            services_sentry._client()


class TestSentryToolCalls(TestCase):
    def test_resolve_requires_a_live_merged_pr_and_verifies_the_issue(self) -> None:
        class Sentry:
            def __init__(self) -> None:
                self.status = "unresolved"
                self.writes = 0

            def resolve_issue(self, issue_id: str) -> dict[str, object]:
                self.writes += 1
                self.status = "resolved"
                return {"id": issue_id, "status": self.status}

            def get_issue(self, issue_id: str) -> dict[str, object]:
                return {"id": issue_id, "status": self.status, "permalink": "https://sentry.io/acme/issues/9/"}

        sentry = Sentry()
        forge = MagicMock()
        forge.fetch_pr_merge_state.return_value = PrMergeState(state="MERGED", merge_commit_oid="abc")
        forge.fetch_pr_message.return_value = PrMessage("Fix error", "Fixes https://sentry.io/acme/issues/9/")
        with (
            patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SentryOverlay()}),
            patch("teatree.mcp.services_sentry._client", return_value=sentry),
            patch("teatree.mcp.services_sentry._forge_client", return_value=forge),
        ):
            result = structured(
                async_to_sync(build_server().call_tool)(
                    "sentry_issue_resolve",
                    {"issue_id": "9", "forge": "github", "repo": "acme/widgets", "pr_iid": 7},
                )
            )

        assert result == {"id": "9", "status": "resolved", "fixing_pr_merge_oid": "abc"}
        assert sentry.writes == 1
        forge.fetch_pr_merge_state.assert_called_once_with(slug="acme/widgets", pr_id=7)
        forge.fetch_pr_message.assert_called_once_with(slug="acme/widgets", pr_id=7)

    def test_resolve_refuses_a_merged_pr_with_no_link_to_the_issue(self) -> None:
        sentry = MagicMock()
        sentry.get_issue.return_value = {
            "id": "9",
            "status": "unresolved",
            "permalink": "https://sentry.io/acme/issues/9/",
        }
        forge = MagicMock()
        forge.fetch_pr_merge_state.return_value = PrMergeState(state="MERGED", merge_commit_oid="abc")
        forge.fetch_pr_message.return_value = PrMessage("Unrelated change", "No issue link")
        with (
            patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SentryOverlay()}),
            patch("teatree.mcp.services_sentry._client", return_value=sentry),
            patch("teatree.mcp.services_sentry._forge_client", return_value=forge),
            pytest.raises(ToolError, match="link"),
        ):
            async_to_sync(build_server().call_tool)(
                "sentry_issue_resolve", {"issue_id": "9", "forge": "github", "repo": "acme/widgets", "pr_iid": 7}
            )
        sentry.resolve_issue.assert_not_called()

    def test_resolve_refuses_an_unmerged_pr_before_sentry_write(self) -> None:
        sentry = MagicMock()
        forge = MagicMock()
        forge.fetch_pr_merge_state.return_value = PrMergeState(state="OPEN", merge_commit_oid="")
        with (
            patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SentryOverlay()}),
            patch("teatree.mcp.services_sentry._client", return_value=sentry),
            patch("teatree.mcp.services_sentry._forge_client", return_value=forge),
            pytest.raises(ToolError, match="merged"),
        ):
            async_to_sync(build_server().call_tool)(
                "sentry_issue_resolve", {"issue_id": "9", "forge": "github", "repo": "acme/widgets", "pr_iid": 7}
            )
        sentry.resolve_issue.assert_not_called()

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

        sentry_reads = [
            tool for name, tool in tools.items() if name.startswith("sentry_") and name != "sentry_issue_resolve"
        ]
        assert len(sentry_reads) == 4
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in sentry_reads)
        assert tools["sentry_issue_resolve"].annotations.read_only_hint is False
