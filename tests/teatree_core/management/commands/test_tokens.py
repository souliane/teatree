r"""The ``tokens`` management command wiring (``teatree.core.management.commands.tokens``).

The end-to-end ``call_command('tokens')`` behaviour lives in ``tests/test_token_report.py``;
this file exercises the ``Command`` class directly — its framework wiring and that its
``handle`` emits through the machine-output seam and returns the typed rows.
"""

import io
import json

from django.test import TestCase
from django_typer.management import TyperCommand

from teatree.core.management.commands.tokens import Command
from teatree.token_report import TokenAccountPayload


class TokensCommandWiringTest(TestCase):
    def test_command_is_a_typer_command(self) -> None:
        assert issubclass(Command, TyperCommand)

    @staticmethod
    def _run(*, json_output: bool) -> tuple[list[TokenAccountPayload], str, str]:
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        rows = command.handle(json_output=json_output)
        return rows, command.stdout._out.getvalue(), command.stderr._out.getvalue()

    def test_handle_renders_the_placeholder_on_stderr_when_nothing_is_configured(self) -> None:
        rows, out, err = self._run(json_output=False)
        assert rows == []
        assert out == ""
        assert "No Anthropic accounts configured" in err

    def test_handle_json_puts_an_empty_document_on_stdout(self) -> None:
        rows, out, err = self._run(json_output=True)
        assert rows == []
        assert json.loads(out) == []
        assert err == ""
