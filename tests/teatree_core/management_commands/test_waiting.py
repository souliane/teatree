"""The ``t3 teatree waiting`` management command (PR-21).

``list`` prints every entry waiting on the user; ``add`` records a manual
item; ``resolve`` closes a manual item by id.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.management.commands.waiting import WaitingPayload
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.waiting_item import WaitingItem
from teatree.core.waiting import WaitingKind

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str) -> str:
    """Both channels merged: these are CONTENT tests, not channel tests.

    Converted verbs route their human view to stderr through the machine-output
    seam while unconverted siblings still return it for stdout, so a content
    assertion must read both. The channel split itself is asserted by the
    dedicated tests below and by ``tests/quality/test_machine_output_seam.py``.
    """
    buf = StringIO()
    call_command(*args, stdout=buf, stderr=buf)
    return buf.getvalue()


class TestList:
    def test_empty_reports_nothing_waiting(self) -> None:
        assert "nothing waiting" in _call("waiting", "list").lower()

    def test_lists_every_kind(self) -> None:
        DeferredQuestion.record("what region?")
        WaitingItem.objects.add("chase finance")
        out = _call("waiting", "list")
        assert "question" in out
        assert "manual" in out
        assert "chase finance" in out

    def test_json_output(self) -> None:
        WaitingItem.objects.add("call the bank")
        payload = json.loads(_call("waiting", "list", "--json"))
        assert payload["count"] == 1
        assert payload["entries"][0]["kind"] == WaitingKind.MANUAL
        assert payload["entries"][0]["ref"] == "call the bank"


class TestAdd:
    def test_add_records_manual_item(self) -> None:
        out = _call("waiting", "add", "review the contract")
        assert WaitingItem.objects.open().count() == 1
        assert "review the contract" in out or "recorded" in out.lower()


class TestResolve:
    def test_resolve_closes_the_item(self) -> None:
        item = WaitingItem.objects.add("done soon")
        out = _call("waiting", "resolve", str(item.pk))
        assert WaitingItem.objects.open().count() == 0
        assert "resolved" in out.lower()

    def test_resolve_absent_reports_no_open_item(self) -> None:
        out = _call("waiting", "resolve", "999999")
        assert "no open" in out.lower()


class TestMachineOutputChannel:
    """``waiting list`` is a seam command: stdout carries JSON or nothing at all."""

    @staticmethod
    def _channels(*args: str) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        call_command("waiting", "list", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_human_mode_leaves_stdout_empty(self) -> None:
        out, err = self._channels()
        assert out == ""
        assert err != ""

    def test_json_mode_puts_only_json_on_stdout(self) -> None:
        out, err = self._channels("--json")
        assert json.loads(out) is not None
        assert err == ""

    def test_list_returns_exactly_the_declared_payload_shape(self) -> None:
        payload = call_command("waiting", "list", stdout=StringIO(), stderr=StringIO())
        assert set(payload) == set(WaitingPayload.__annotations__)
