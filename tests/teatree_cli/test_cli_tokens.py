r"""The top-level ``t3 tokens`` CLI command (``teatree.cli.tokens``).

A thin convenience over the ``tokens`` management command — like ``t3 cost`` — so the
test asserts it bootstraps Django and delegates the flag through to ``call_command``.
"""

import inspect
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.tokens import tokens

runner = CliRunner()

_app = typer.Typer()
_app.command()(tokens)


class TestTokensCliDelegation:
    def test_delegates_to_the_management_command(self) -> None:
        with (
            patch("teatree.cli.tokens.ensure_django") as ensure_mock,
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, [])
        assert result.exit_code == 0
        ensure_mock.assert_called_once_with()
        call_mock.assert_called_once_with("tokens", json_output=False, tokens=None)

    def test_passes_the_json_flag(self) -> None:
        with (
            patch("teatree.cli.tokens.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["--json"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("tokens", json_output=True, tokens=None)

    def test_passes_repeated_token_options_in_order(self) -> None:
        with (
            patch("teatree.cli.tokens.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["--token", "sk-ant-oat01-A", "--token", "sk-ant-api03-B"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("tokens", json_output=False, tokens=["sk-ant-oat01-A", "sk-ant-api03-B"])

    def test_token_option_help_warns_about_command_line_exposure(self) -> None:
        option = inspect.signature(tokens).parameters["tokens"].default
        help_text = option.help.lower()
        assert "ps" in help_text
        assert "history" in help_text
