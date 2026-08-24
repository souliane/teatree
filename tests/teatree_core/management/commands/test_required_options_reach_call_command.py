"""Gate commands must be invocable through ``call_command`` with plain kwargs.

An ``Annotated[str, typer.Option(...)]`` parameter with no default raises
``Missing parameter`` under ``call_command`` even when the caller passes the kwarg, so
every in-process caller had to hand-roll raw argv. These are the gate commands an
in-process caller reaches for; each takes a default plus a runtime non-blank check.
"""

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestNotifyAcceptsKwargs(TestCase):
    def test_send_reaches_the_idempotency_key_check_via_kwargs(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            call_command("notify", "send", "a body", idempotency_key="")
        assert exit_info.value.code == 2

    def test_post_reaches_the_channel_check_via_kwargs(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            call_command("notify", "post", channel="", text="hello")
        assert exit_info.value.code == 2

    def test_react_reaches_the_ts_check_via_kwargs(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            call_command("notify", "react", channel="C1", ts="", emoji="eyes")
        assert exit_info.value.code == 2


class TestTicketGateCommandsAcceptKwargs(TestCase):
    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)

    def test_dod_override_refuses_a_blank_reason_via_kwargs(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            call_command("ticket", "dod-override", self._ticket().pk, reason="  ")
        assert exit_info.value.code == 1

    def test_dod_override_records_via_kwargs(self) -> None:
        ticket = self._ticket()
        call_command("ticket", "dod-override", ticket.pk, reason="non-UI ticket")
        ticket.refresh_from_db()
        assert ticket.extra["dod_e2e_override"]["reason"] == "non-UI ticket"

    def test_e2e_bypass_refuses_a_missing_approver_via_kwargs(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            call_command("ticket", "e2e-bypass", self._ticket().pk, approver="", head_sha="a" * 40)
        assert exit_info.value.code == 1
