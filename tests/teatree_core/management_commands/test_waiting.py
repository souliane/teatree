"""The ``t3 teatree waiting`` management command (PR-21).

``list`` prints every entry waiting on the user; ``add`` records a manual
item; ``resolve`` closes a manual item by id.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.waiting_item import WaitingItem
from teatree.core.waiting import WaitingKind

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
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
