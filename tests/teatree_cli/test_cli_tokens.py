r"""The top-level ``t3 tokens`` CLI command (``teatree.cli.tokens``).

A thin convenience over the ``tokens`` management command — like ``t3 cost`` — so the
test asserts it bootstraps Django and delegates the flag through to ``call_command``.
"""

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
        call_mock.assert_called_once_with("tokens", json_output=False)

    def test_passes_the_json_flag(self) -> None:
        with (
            patch("teatree.cli.tokens.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["--json"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("tokens", json_output=True)
