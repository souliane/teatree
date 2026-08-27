"""Gating fitness tests for the per-service MCP tool groups (#3076).

Each forge/messaging/notion group registers iff a registered overlay declares
its ``Service`` — the same fail-closed contract the Sentry pilot pins.
"""

import asyncio
from unittest.mock import patch

from django.test import TestCase
from mcp.server.mcpserver import MCPServer

from teatree.backends.types import Service
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp import build_server
from teatree.mcp.server import _SERVICE_GROUPS
from teatree.mcp.services_forge import register_github, register_gitlab
from teatree.mcp.services_notion import register as register_notion
from teatree.mcp.services_sentry import register as register_sentry
from teatree.mcp.services_sharepoint import register as register_sharepoint
from teatree.mcp.services_slack import register

_GITHUB_TOOLS = {
    "github_current_user",
    "github_my_prs",
    "github_review_requested",
    "github_pr_author",
    "github_pr_comments",
    "github_issue",
    "github_issue_comments",
    "github_issue_search",
    "github_issue_list_assigned",
    "github_my_merged_prs",
    "github_pr_get",
    "github_pr_list",
    "github_pr_diff",
    "github_pr_commits",
    "github_repo_get",
    "github_issue_create",
    "github_issue_comment",
    "github_issue_close",
    "github_issue_update",
}
_GITLAB_TOOLS = {n.replace("github_", "gitlab_") for n in _GITHUB_TOOLS}
_SLACK_TOOLS = {
    "slack_mentions",
    "slack_channel_history",
    "slack_thread_replies",
    "slack_permalink",
    "slack_react",
}
_NOTION_TOOLS = {"notion_page_status"}
_SENTRY_TOOLS = {"sentry_top_issues", "sentry_issue_get", "sentry_issue_events", "sentry_projects"}
_SHAREPOINT_TOOLS = {
    "sharepoint_list",
    "sharepoint_cat",
    "sharepoint_verify_link",
    "sharepoint_verify_read_only",
}

_GROUP_BY_SERVICE = {
    Service.GITHUB: _GITHUB_TOOLS,
    Service.GITLAB: _GITLAB_TOOLS,
    Service.SLACK: _SLACK_TOOLS,
    Service.NOTION: _NOTION_TOOLS,
    Service.SENTRY: _SENTRY_TOOLS,
    Service.SHAREPOINT: _SHAREPOINT_TOOLS,
}
_ALL_SERVICE_TOOLS = set().union(*_GROUP_BY_SERVICE.values())

_REGISTRAR_BY_SERVICE = {
    Service.GITHUB: register_github,
    Service.GITLAB: register_gitlab,
    Service.SLACK: register,
    Service.NOTION: register_notion,
    Service.SENTRY: register_sentry,
    Service.SHAREPOINT: register_sharepoint,
}


class _ServiceOverlay:
    def __init__(self, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))
        self.connectors = OverlayConnectors()


def _tools_for(*services: Service) -> set[str]:
    with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay(*services)}):
        return {tool.name for tool in asyncio.run(build_server().list_tools())}


class TestServiceGroupGating(TestCase):
    def test_declared_service_registers_only_its_group(self) -> None:
        for service, expected in _GROUP_BY_SERVICE.items():
            with self.subTest(service=service.value):
                names = _tools_for(service)
                assert expected <= names
                assert not (_ALL_SERVICE_TOOLS - expected) & names

    def test_no_declaration_registers_no_service_tools(self) -> None:
        assert not _ALL_SERVICE_TOOLS & _tools_for()


class TestRegistrarsBindToTheServerType(TestCase):
    """Each registrar puts its group on a bare server of the type `build_server` builds.

    Reached only through `_SERVICE_GROUPS`, a registrar whose server binding
    broke surfaces as a missing tool name — the same symptom as a gating bug.
    Calling each one directly names the binding instead.
    """

    def test_each_registrar_registers_its_group(self) -> None:
        for service, registrar in _REGISTRAR_BY_SERVICE.items():
            with self.subTest(service=service.value):
                server = MCPServer("test")
                registrar(server)
                assert _GROUP_BY_SERVICE[service] <= {tool.name for tool in asyncio.run(server.list_tools())}

    def test_the_server_wires_those_same_registrars(self) -> None:
        assert {service: registrar for service, (registrar, _) in _SERVICE_GROUPS.items()} == _REGISTRAR_BY_SERVICE
