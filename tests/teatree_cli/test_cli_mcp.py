"""Tests for the ``t3 mcp serve`` CLI command."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.mcp import serve

runner = CliRunner()

_app = typer.Typer()
_app.command()(serve)


class TestServeCommand:
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
