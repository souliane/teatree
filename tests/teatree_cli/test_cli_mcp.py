"""Tests for the ``t3 mcp serve`` CLI command."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from typer.testing import CliRunner

from teatree.cli.mcp import serve
from teatree.mcp import services_notion
from teatree.mcp.server import build_server

runner = CliRunner()

_app = typer.Typer()
_app.command()(serve)


class TestServeCommand:
    def test_the_notion_write_seam_is_registered_before_the_server_is_built(self) -> None:
        factories = []
        original = services_notion._factory_registry.factory
        services_notion.register_notion_write_seam(services_notion._unregistered_factory)
        try:
            with (
                patch("teatree.cli.mcp.ensure_django"),
                patch(
                    "teatree.mcp.server.build_server",
                    side_effect=lambda: factories.append(services_notion._factory_registry.factory),
                ),
            ):
                runner.invoke(_app, [])
        finally:
            services_notion.register_notion_write_seam(original)

        assert len(factories) == 1
        assert factories[0] is not services_notion._unregistered_factory

    def test_bootstraps_django_then_runs_stdio_server(self) -> None:
        with (
            patch("teatree.cli.mcp.ensure_django") as ensure_mock,
            patch("teatree.mcp.server.build_server") as build_mock,
        ):
            result = runner.invoke(_app, [])

        assert result.exit_code == 0
        ensure_mock.assert_called_once_with()
        build_mock.assert_called_once_with()
        build_mock.return_value.run.assert_called_once_with("stdio")


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
_SERVE_DRIVER = "from teatree.cli.mcp import serve; serve()"


def _clean_env(data_home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env["XDG_DATA_HOME"] = str(data_home)
    env["PYTHONPATH"] = os.pathsep.join([str(_SRC_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["DJANGO_SETTINGS_MODULE"] = "teatree.settings"
    return env


def _migrate(env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "teatree", "migrate", "--no-input"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


async def _round_trip(env: dict[str, str]) -> tuple[set[str], dict]:
    """Drive a real MCP client against a real ``t3 mcp serve`` subprocess over stdio."""
    params = StdioServerParameters(command=sys.executable, args=["-c", _SERVE_DRIVER], env=env, cwd=str(_REPO_ROOT))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("loop_stats", {})
        payload = json.loads(result.content[0].text)
        return {tool.name for tool in tools.tools}, payload


@pytest.mark.integration
@pytest.mark.timeout(180)
class TestServeSubprocessSmoke:
    """End-to-end proof that an external MCP client can talk to ``t3 mcp serve``.

    Everything else in this module drives ``serve()`` in-process (mocked
    ``build_server``/``ensure_django``) or ``build_server()`` directly
    (``tests/teatree_mcp/test_server.py``) — neither exercises the actual
    stdio transport a real MCP client speaks. This spawns the CLI entry point
    as a genuine subprocess against an isolated, migrated SQLite DB and drives
    it with the official ``mcp`` client SDK (#2863).
    """

    def test_lists_tools_and_calls_one_over_real_stdio(self, tmp_path: Path) -> None:
        env = _clean_env(tmp_path / "xdg")
        _migrate(env)

        tool_names, payload = asyncio.run(_round_trip(env))

        # Transport parity: the surface an external stdio client sees is exactly
        # the surface build_server() registers in-process (registration content
        # itself is pinned in tests/teatree_mcp/test_server.py).
        in_process = {tool.name for tool in asyncio.run(build_server().list_tools())}
        assert tool_names == in_process
        assert {"command_search", "loop_stats", "pr_create", "review_post_comment"} <= tool_names
        assert payload == {
            "overlay": "",
            "tasks": {"pending": 0, "claimed": 0, "completed": 0, "failed": 0},
            "dead_letter": 0,
        }
