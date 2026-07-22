"""Tests for ``manage.py ticket_short_describe`` (#1156).

The command is a thin CLI wrapper over the summariser in
:mod:`teatree.agents.short_describe`; those tests live in
``tests/teatree_agents/test_short_describe.py``. Here we cover only the command's
argument-validation branches and that each flag reaches the right summariser entrypoint.
The ``_summarize`` patch keeps the suite off a real LLM.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket

_SUMMARIZE = "teatree.agents.short_describe._summarize"


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestCommandDescribeMethod(TestCase):
    """Cover the ``Command.describe`` argument-validation branches directly."""

    def _command(self):
        from teatree.core.management.commands.ticket_short_describe import Command  # noqa: PLC0415

        return Command()

    def test_describe_rejects_both_flags(self) -> None:
        cmd = self._command()
        with pytest.raises(SystemExit) as excinfo:
            cmd.describe(ticket_id=1, all_missing=True)
        assert excinfo.value.code == 2

    def test_describe_rejects_no_flags(self) -> None:
        cmd = self._command()
        with pytest.raises(SystemExit) as excinfo:
            cmd.describe(ticket_id=0, all_missing=False)
        assert excinfo.value.code == 2

    def test_describe_with_ticket_id_calls_describe_ticket(self) -> None:
        cmd = self._command()
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "test ticket"},
        )
        with patch(_SUMMARIZE, return_value="test ticket"):
            cmd.describe(ticket_id=ticket.pk, all_missing=False)
        ticket.refresh_from_db()
        assert ticket.short_description == "test ticket"

    def test_describe_with_all_missing_calls_backfill(self) -> None:
        cmd = self._command()
        Ticket.objects.create(
            overlay="t3-teatree",
            extra={"issue_title": "backfill candidate"},
        )
        with patch(_SUMMARIZE, return_value="backfilled"):
            cmd.describe(ticket_id=0, all_missing=True)
