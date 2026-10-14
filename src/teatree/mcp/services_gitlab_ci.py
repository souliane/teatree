"""GitLab CI reads through the configured CI service."""

from asgiref.sync import sync_to_async
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from teatree.core.backend_protocols import PipelineRead
from teatree.mcp.service_resolver import GITLAB_CI

_READ_ONLY = ToolAnnotations(read_only_hint=True)

INSTRUCTIONS = (
    "- gitlab_ci_errors(project, ref): failed-job logs from the latest pipeline; an unreadable pipeline is an error.\n"
    "- gitlab_ci_failed_tests(project, ref): failed test IDs from the latest pipeline; "
    "an unreadable report is an error."
)


def _findings(read: PipelineRead) -> list[str]:
    if not read.ok:
        raise ToolError(read.unreadable_reason)
    return list(read.findings)


async def _errors(project: str, ref: str) -> list[str]:
    read = await sync_to_async(
        lambda: GITLAB_CI.resolve().fetch_pipeline_errors(project=project, ref=ref), thread_sensitive=True
    )()
    return _findings(read)


async def _failed_tests(project: str, ref: str) -> list[str]:
    read = await sync_to_async(
        lambda: GITLAB_CI.resolve().fetch_failed_tests(project=project, ref=ref), thread_sensitive=True
    )()
    return _findings(read)


def register(server: MCPServer) -> None:
    server.add_tool(_errors, name="gitlab_ci_errors", annotations=_READ_ONLY)
    server.add_tool(_failed_tests, name="gitlab_ci_failed_tests", annotations=_READ_ONLY)
