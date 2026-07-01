r"""The ``tokens`` management command wiring (``teatree.core.management.commands.tokens``).

The end-to-end ``call_command('tokens')`` behaviour lives in ``tests/test_token_report.py``;
this file exercises the ``Command`` class directly — its framework wiring and that its
``handle`` returns the rendered report django-typer serialises.
"""

from django.test import TestCase
from django_typer.management import TyperCommand

from teatree.core.management.commands.tokens import Command


class TokensCommandWiringTest(TestCase):
    def test_command_is_a_typer_command(self) -> None:
        assert issubclass(Command, TyperCommand)

    def test_handle_renders_the_placeholder_when_nothing_is_configured(self) -> None:
        assert "No Anthropic accounts configured" in Command().handle(json_output=False)

    def test_handle_json_returns_an_empty_json_document_when_nothing_is_configured(self) -> None:
        assert Command().handle(json_output=True) == "[]"
