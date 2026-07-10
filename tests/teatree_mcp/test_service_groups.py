"""Gating fitness tests for the per-service MCP tool groups (#3076).

Each forge/messaging/notion group registers iff a registered overlay declares
its ``Service`` — the same fail-closed contract the Sentry pilot pins.
"""

import asyncio
from unittest.mock import patch

from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.overlay import OverlayConfig
from teatree.mcp import build_server

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
}
_GITLAB_TOOLS = {n.replace("github_", "gitlab_") for n in _GITHUB_TOOLS}
_SLACK_TOOLS = {"slack_mentions", "slack_channel_history", "slack_thread_replies", "slack_permalink"}
_NOTION_TOOLS = {"notion_page_status"}
_SENTRY_TOOLS = {"sentry_top_issues", "sentry_issue_get", "sentry_issue_events", "sentry_projects"}

_GROUP_BY_SERVICE = {
    Service.GITHUB: _GITHUB_TOOLS,
    Service.GITLAB: _GITLAB_TOOLS,
    Service.SLACK: _SLACK_TOOLS,
    Service.NOTION: _NOTION_TOOLS,
    Service.SENTRY: _SENTRY_TOOLS,
}
_ALL_SERVICE_TOOLS = set().union(*_GROUP_BY_SERVICE.values())


class _ServiceOverlay:
    def __init__(self, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))


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
