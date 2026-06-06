"""The ``t3 <overlay> ticket`` group exposes the plan escapes (#1977).

``plan-bypass`` / ``plan-reconcile-inflight`` lived on ``manage.py ticket`` but
were never listed in ``DJANGO_GROUPS['ticket']``, so ``t3 <overlay> ticket
plan-bypass`` did not bridge through. And the ``NoPlanArtifactError`` message
names ``t3 <overlay> ticket plan <id> "<text>"`` — a command that must resolve
against the wired surface, not a phantom name.
"""

import pytest
from django.test import SimpleTestCase, TestCase

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.core.gates.plan_gate import check_plan_artifact
from teatree.core.models import Ticket

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class TicketGroupWiringTest(SimpleTestCase):
    def _ticket_subcommands(self) -> set[str]:
        return {name for name, _help in DJANGO_GROUPS["ticket"].subcommands}

    def test_plan_recorder_is_wired(self) -> None:
        assert "plan" in self._ticket_subcommands()

    def test_plan_bypass_is_wired(self) -> None:
        assert "plan-bypass" in self._ticket_subcommands()

    def test_plan_reconcile_inflight_is_wired(self) -> None:
        assert "plan-reconcile-inflight" in self._ticket_subcommands()


class NoPlanArtifactMessageResolvesTest(TestCase):
    def test_error_message_names_a_wired_ticket_subcommand(self) -> None:
        ticket = Ticket.objects.create(pk=123, overlay="test", state=Ticket.State.STARTED)
        with pytest.raises(Exception) as exc:  # noqa: PT011 - assert on message
            check_plan_artifact(ticket)
        msg = str(exc.value)
        # The message must name a ticket subcommand that is actually wired.
        wired = {name for name, _h in DJANGO_GROUPS["ticket"].subcommands}
        named = {sub for sub in wired if f"ticket {sub} " in msg or f"ticket {sub}<" in msg}
        assert named, f"error message names no wired ticket subcommand: {msg!r}"
