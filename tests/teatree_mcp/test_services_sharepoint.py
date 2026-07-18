"""Tests for the read-only SharePoint MCP tool group (#3084).

The client is resolved from the first registered overlay declaring
``Service.SHAREPOINT`` (with the remote configured via the ``TEATREE_SHAREPOINT_*``
environment) through the ``sharepoint_client_from_overlay`` core seam (no direct
``teatree.backends.sharepoint`` import in ``teatree.mcp``); the client itself is
the only mock, so no ``rclone`` runs.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.overlay import OverlayConfig
from teatree.mcp import build_server, services_sharepoint


class _SharePointOverlay:
    def __init__(self) -> None:
        self.config = OverlayConfig(
            required_third_party_services=frozenset({Service.SHAREPOINT}),
        )


class TestSharePointClientResolution(TestCase):
    def test_resolves_client_from_declaring_overlay(self) -> None:
        built = MagicMock()
        with (
            patch("teatree.mcp.service_resolver.get_all_overlays", return_value={"a": _SharePointOverlay()}),
            patch("teatree.mcp.services_sharepoint.sharepoint_client_from_overlay", return_value=built) as build,
        ):
            client = services_sharepoint._client()

        assert client is built
        build.assert_called_once_with("a")

    def test_no_configured_declarer_fails_loud(self) -> None:
        # The overlay declares SharePoint, but its factory build yields no
        # client (TEATREE_SHAREPOINT_REMOTE unset) — the resolver falls through.
        with (
            patch("teatree.mcp.service_resolver.get_all_overlays", return_value={"a": _SharePointOverlay()}),
            patch("teatree.mcp.services_sharepoint.sharepoint_client_from_overlay", return_value=None),
            pytest.raises(RuntimeError, match="SharePoint document library"),
        ):
            services_sharepoint._client()


class TestSharePointToolCalls(TestCase):
    def test_list_routes_through_the_backend_client(self) -> None:
        fake = MagicMock()
        fake.list_files.return_value = [{"Name": "spec.md", "IsDir": False}]
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SharePointOverlay()}):
            server = build_server()
        with patch("teatree.mcp.services_sharepoint._client", return_value=fake):
            result = async_to_sync(server.call_tool)("sharepoint_list", {"subpath": "Specs"})

        fake.list_files.assert_called_once_with("Specs", recursive=True)
        assert result

    def test_each_tool_reaches_its_client_read(self) -> None:
        fake = MagicMock()
        fake.cat.return_value = "body"
        fake.verify_link.return_value = {"path": "Specs", "url": "https://x/?id=/Specs", "exists": True}
        fake.verify_read_only.return_value = True
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SharePointOverlay()}):
            server = build_server()

        with patch("teatree.mcp.services_sharepoint._client", return_value=fake):
            async_to_sync(server.call_tool)("sharepoint_cat", {"file_path": "Specs/spec.md"})
            async_to_sync(server.call_tool)("sharepoint_verify_link", {"folder_path": "Specs"})
            async_to_sync(server.call_tool)("sharepoint_verify_read_only", {})

        fake.cat.assert_called_once_with("Specs/spec.md")
        fake.verify_link.assert_called_once_with("Specs")
        fake.verify_read_only.assert_called_once_with()

    def test_registered_sharepoint_tools_are_read_only(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _SharePointOverlay()}):
            tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}

        sp_tools = [tool for name, tool in tools.items() if name.startswith("sharepoint_")]
        assert len(sp_tools) == 4
        assert all(tool.annotations and tool.annotations.readOnlyHint for tool in sp_tools)
