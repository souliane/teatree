"""Startup guards for the ``t3 mcp serve`` stdio server.

The MCP client spawns this server ONCE at session start, times out the
``initialize`` handshake, and never retries — so both a crashing import and a
merely slow one surface as the same opaque "connection closed"/"request timed
out". Each fault below therefore gets a test rather than a doctor WARN, because
a WARN sitting among twenty benign host/container-boundary WARNs is not a signal.

Startup cost is asserted as an IMPORT-GRAPH property, not a wall clock: this
server also runs inside a saturated worker container where elapsed time swings
by tens of seconds between identical runs, so a timing budget would be flaky
while the set of imported modules stays exact.
"""

import importlib.metadata
import json
import subprocess
import sys
from typing import NamedTuple

import pytest

# Agent-harness SDKs that must never load merely to SERVE the MCP tool surface.
# `openai` arrives via `pydantic_ai.models.openai` and cost ~18s of every `t3`
# invocation when `AgentsConfig.ready()` imported the harness eagerly.
FORBIDDEN_STARTUP_MODULES = ("openai", "pydantic_ai", "litellm", "anthropic")

# Ceiling on the whole import graph, so a NEW heavy dependency cannot slip in
# under a different name than the four above. Measured at 2146 after the
# deferral; the headroom absorbs ordinary growth without hiding a regression.
MAX_STARTUP_MODULES = 2400

_PROBE = """
import json, sys

from teatree.utils.django_bootstrap import ensure_django

ensure_django()
import teatree.cli  # noqa: F401 — the CLI surface `t3 mcp serve` loads before dispatching
from teatree.mcp.server import build_server

build_server()
forbidden = {forbidden!r}
print(json.dumps({{
    "loaded": sorted(m for m in forbidden if m in sys.modules),
    "count": len(sys.modules),
}}))
"""


class StartupImportGraph(NamedTuple):
    """What a clean interpreter loaded to build the MCP server."""

    forbidden_loaded: tuple[str, ...]
    module_count: int


def _startup_import_graph() -> StartupImportGraph:
    """Build the MCP server in a CLEAN interpreter and report what it imported.

    A subprocess is load-bearing: the pytest process has already imported the
    agent harness via other tests, so an in-process ``sys.modules`` check would
    pass no matter what this entry point does.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(forbidden=FORBIDDEN_STARTUP_MODULES)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"MCP server failed to build:\n{completed.stderr[-4000:]}")
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    return StartupImportGraph(
        forbidden_loaded=tuple(payload["loaded"]),
        module_count=int(payload["count"]),
    )


class TestMcpServeStartupBudget:
    """Asserts import COMPOSITION, so the suite's wall-clock timeout is lifted here.

    Building the server in a cold interpreter is inherently slow on a loaded box;
    the default per-test timeout would kill the probe before it could assert, turning
    a precise "these SDKs loaded" failure into an uninformative timeout.
    """

    @pytest.mark.timeout(300)
    def test_serving_mcp_does_not_import_the_agent_sdks(self) -> None:
        graph = _startup_import_graph()

        assert graph.forbidden_loaded == (), (
            f"`t3 mcp serve` imported {list(graph.forbidden_loaded)} — these SDKs cost "
            "seconds of startup and the MCP tool surface never calls them. Register "
            "runners lazily (see teatree.agents.apps) instead of importing at django.setup()."
        )

    @pytest.mark.timeout(300)
    def test_startup_import_graph_stays_under_the_ceiling(self) -> None:
        graph = _startup_import_graph()

        assert graph.module_count <= MAX_STARTUP_MODULES, (
            f"`t3 mcp serve` imported {graph.module_count} modules, over the "
            f"{MAX_STARTUP_MODULES} ceiling — a new heavy dependency reached CLI "
            "startup. Defer it to its first call site."
        )


class TestMcpSdkEnvironmentParity:
    """The stale-environment fault: a declared-but-not-installed SDK major version.

    ``teatree.mcp.server`` imports ``mcp.server.mcpserver.MCPServer`` (SDK 2.x). A
    tool environment left on 1.x raises ``ModuleNotFoundError`` at import, so the
    server exits before writing a single byte and the client reports only a closed
    connection. The declaration was always correct; nothing verified the env matched it.
    """

    def test_installed_mcp_sdk_provides_the_imported_symbol(self) -> None:
        try:
            from mcp.server.mcpserver import MCPServer  # noqa: PLC0415 — the import under test
        except ModuleNotFoundError as exc:  # pragma: no cover — only on a stale env
            installed = importlib.metadata.version("mcp")
            pytest.fail(
                f"installed mcp SDK {installed} lacks mcp.server.mcpserver ({exc}). "
                "Rebuild the environment: "
                "`uv tool install --editable . --overrides uv-overrides.txt --reinstall`, "
                "then `/reload-plugins` — Claude Code spawns the server once per session "
                "and will not reconnect a repaired binary on its own."
            )

        assert MCPServer is not None

    def test_installed_mcp_sdk_is_major_version_two(self) -> None:
        installed = importlib.metadata.version("mcp")
        major = int(installed.split(".", maxsplit=1)[0])

        assert major == 2, (
            f"installed mcp SDK is {installed}; teatree.mcp.server targets the 2.x API. "
            "Reinstall the tool environment — see this class's docstring."
        )
