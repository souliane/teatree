"""Tests for t3 plugin install CLI command."""

from subprocess import CompletedProcess
from unittest.mock import patch

import typer
from typer.testing import CliRunner

import teatree.cli.plugin as cli_plugin_mod
from teatree.cli import app
from teatree.cli.plugin import (
    _ensure_marketplace,
    _teatree_root,
    _try_apm_install,
    _try_claude_plugin_install,
    plugin_app,
)


def test_teatree_root_points_to_repo():
    root = _teatree_root()
    assert (root / "pyproject.toml").is_file()


def test_try_apm_install_no_apm():
    with patch("shutil.which", return_value=None):
        assert _try_apm_install() is False


def test_try_apm_install_success():
    with (
        patch("shutil.which", return_value="/usr/bin/apm"),
        patch.object(
            cli_plugin_mod.subprocess,
            "run",
            return_value=CompletedProcess(args=[], returncode=0),
        ),
    ):
        assert _try_apm_install() is True


def test_try_apm_install_failure():
    with (
        patch("shutil.which", return_value="/usr/bin/apm"),
        patch.object(
            cli_plugin_mod.subprocess,
            "run",
            return_value=CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert _try_apm_install() is False


def test_try_claude_plugin_install_no_claude():
    with patch("shutil.which", return_value=None):
        assert _try_claude_plugin_install(scope="user", dev=False) is False


def test_try_claude_plugin_install_dev_mode():
    with patch("shutil.which", return_value="/usr/bin/claude"):
        assert _try_claude_plugin_install(scope="user", dev=True) is True


def test_try_claude_plugin_install_success():
    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch.object(
            cli_plugin_mod.subprocess,
            "run",
            return_value=CompletedProcess(args=[], returncode=0),
        ),
    ):
        assert _try_claude_plugin_install(scope="user", dev=False) is True


def test_try_claude_plugin_install_marketplace_fails():
    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch.object(
            cli_plugin_mod.subprocess,
            "run",
            return_value=CompletedProcess(args=[], returncode=1, stderr="error"),
        ),
    ):
        assert _try_claude_plugin_install(scope="user", dev=False) is False


def test_try_claude_plugin_install_marketplace_ok_install_fails():
    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch.object(
            cli_plugin_mod.subprocess,
            "run",
            side_effect=[
                CompletedProcess(args=[], returncode=0),  # marketplace add
                CompletedProcess(args=[], returncode=1, stderr="install error"),  # plugin install
            ],
        ),
    ):
        assert _try_claude_plugin_install(scope="user", dev=False) is False


def test_ensure_marketplace_already_exists():
    with patch.object(
        cli_plugin_mod.subprocess,
        "run",
        return_value=CompletedProcess(args=[], returncode=1, stderr="already added"),
    ):
        assert _ensure_marketplace("/usr/bin/claude") is True


def test_install_via_apm():
    runner = CliRunner()
    with patch.object(cli_plugin_mod, "_try_apm_install", return_value=True):
        result = runner.invoke(app, ["plugin", "install"])
        assert result.exit_code == 0
        assert "APM" in result.output


def test_install_via_claude_cli():
    runner = CliRunner()
    with (
        patch.object(cli_plugin_mod, "_try_apm_install", return_value=False),
        patch.object(cli_plugin_mod, "_try_claude_plugin_install", return_value=True),
    ):
        result = runner.invoke(app, ["plugin", "install"])
        assert result.exit_code == 0
        assert "Claude CLI" in result.output


def test_install_all_fail():
    runner = CliRunner()
    with (
        patch.object(cli_plugin_mod, "_try_apm_install", return_value=False),
        patch.object(cli_plugin_mod, "_try_claude_plugin_install", return_value=False),
    ):
        result = runner.invoke(app, ["plugin", "install"])
        assert result.exit_code == 1


def test_install_dev_mode_with_claude():
    runner = CliRunner()
    with patch.object(cli_plugin_mod, "_try_claude_plugin_install", return_value=True):
        result = runner.invoke(app, ["plugin", "install", "--dev"])
        assert result.exit_code == 0


def test_install_dev_mode_fallback():
    runner = CliRunner()
    with patch.object(cli_plugin_mod, "_try_claude_plugin_install", return_value=False):
        result = runner.invoke(app, ["plugin", "install", "--dev"])
        assert result.exit_code == 1


def test_plugin_app_is_typer():
    assert isinstance(plugin_app, typer.Typer)
