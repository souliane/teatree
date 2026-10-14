"""GitLab CI MCP reads distinguish an empty pipeline from an unreadable one."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.types import Service
from teatree.core.backend_protocols import PipelineRead
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp.server import build_server
from tests.teatree_mcp._call_tool_result import structured


class _Overlay:
    def __init__(self, service: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({service}))
        self.connectors = OverlayConnectors()


class _CI:
    def __init__(self, result: PipelineRead) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    def fetch_pipeline_errors(self, *, project: str, ref: str) -> PipelineRead:
        self.calls.append(("errors", project, ref))
        return self.result

    def fetch_failed_tests(self, *, project: str, ref: str) -> PipelineRead:
        self.calls.append(("tests", project, ref))
        return self.result


def _call(name: str, ci: _CI) -> tuple[bool, object]:
    with (
        patch("teatree.mcp.server.get_all_overlays", return_value={"demo": _Overlay(Service.GITLAB)}),
        patch("teatree.mcp.services_gitlab_ci.GITLAB_CI", SimpleNamespace(resolve=lambda: ci)),
    ):
        result = async_to_sync(build_server().call_tool)(name, {"project": "acme/widgets", "ref": "main"})
    return bool(result.is_error), result.content[0].text if result.is_error else structured(result)


def test_errors_read_returns_findings_and_routes_project_and_ref() -> None:
    ci = _CI(PipelineRead(findings=("job failed",)))

    failed, body = _call("gitlab_ci_errors", ci)

    assert failed is False
    assert body == ["job failed"]
    assert ci.calls == [("errors", "acme/widgets", "main")]


def test_failed_tests_read_refuses_an_unreadable_pipeline() -> None:
    ci = _CI(PipelineRead(unreadable_reason="no test report"))

    with pytest.raises(ToolError, match="no test report"):
        _call("gitlab_ci_failed_tests", ci)

    assert ci.calls == [("tests", "acme/widgets", "main")]


def test_github_only_server_has_no_gitlab_ci_tools() -> None:
    with patch("teatree.mcp.server.get_all_overlays", return_value={"demo": _Overlay(Service.GITHUB)}):
        tools = {tool.name for tool in async_to_sync(build_server().list_tools)()}

    assert "gitlab_ci_errors" not in tools
    assert "gitlab_ci_failed_tests" not in tools
