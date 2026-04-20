"""Tests for `t3 infra redis {up,down,status}` CLI."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.infra import infra_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestInfraRedisStatus:
    def test_prints_status(self, runner: CliRunner) -> None:
        with patch("teatree.cli.infra.redis_container.status", return_value="running"):
            result = runner.invoke(infra_app, ["redis", "status"])
        assert result.exit_code == 0
        assert "running" in result.output


class TestInfraRedisUp:
    def test_calls_ensure_running(self, runner: CliRunner) -> None:
        with (
            patch("teatree.cli.infra.redis_container.ensure_running") as mock_up,
            patch("teatree.cli.infra.redis_container.status", return_value="running"),
        ):
            result = runner.invoke(infra_app, ["redis", "up"])
        assert result.exit_code == 0
        mock_up.assert_called_once_with()


class TestInfraRedisDown:
    def test_calls_stop(self, runner: CliRunner) -> None:
        with (
            patch("teatree.cli.infra.redis_container.stop") as mock_down,
            patch("teatree.cli.infra.redis_container.status", return_value="missing"),
        ):
            result = runner.invoke(infra_app, ["redis", "down"])
        assert result.exit_code == 0
        mock_down.assert_called_once_with()
