"""``t3 ticket record-spec-coverage`` — the producer for the #2232 DoD manifest.

The gate at ``mark_delivered`` treats a MISSING manifest as a hard block, so
before this command existed ``require_spec_coverage = true`` refused EVERY
delivery: the ON state was "delivery bricked", not "coverage enforced". The
end-to-end property these tests pin is that the flag is now satisfiable — a
ticket whose coverage this command recorded reaches DELIVERED, and an uncovered
AC still does not.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates.spec_coverage_gate import SpecCoverageDodError
from teatree.core.models import Ticket


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.spec_coverage_gate.get_effective_settings",
        return_value=UserSettings(require_spec_coverage=required),
    ):
        yield


def _record(ticket: Ticket, *args: str) -> dict[str, object]:
    return cast("dict[str, object]", call_command("ticket", "record-spec-coverage", str(ticket.pk), *args))


class TestRecordSpecCoverage(TestCase):
    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/1")

    def test_records_the_shape_the_gate_reads(self) -> None:
        ticket = self._ticket()
        result = _record(ticket, "--ac", "AC1=tests/a.py::test_one,tests/a.py::test_two", "--ac", "AC2=tests/b.py::t")
        ticket.refresh_from_db()
        assert ticket.extra["spec_coverage"] == {
            "acceptance_criteria": [
                {"id": "AC1", "tests": ["tests/a.py::test_one", "tests/a.py::test_two"]},
                {"id": "AC2", "tests": ["tests/b.py::t"]},
            ]
        }
        assert result["acceptance_criteria"] == 2
        assert result["uncovered"] == []

    def test_ac_with_no_tests_is_recorded_uncovered(self) -> None:
        ticket = self._ticket()
        result = _record(ticket, "--ac", "AC1=tests/a.py::t", "--ac", "AC2=")
        assert result["uncovered"] == ["AC2"]

    def test_upserts_by_label_across_runs(self) -> None:
        ticket = self._ticket()
        _record(ticket, "--ac", "AC1=", "--ac", "AC2=tests/b.py::t")
        _record(ticket, "--ac", "AC1=tests/a.py::t")
        ticket.refresh_from_db()
        criteria = ticket.extra["spec_coverage"]["acceptance_criteria"]
        assert {ac["id"]: ac["tests"] for ac in criteria} == {
            "AC1": ["tests/a.py::t"],
            "AC2": ["tests/b.py::t"],
        }

    def test_replace_drops_criteria_not_restated(self) -> None:
        ticket = self._ticket()
        _record(ticket, "--ac", "AC1=tests/a.py::t", "--ac", "AC2=tests/b.py::t")
        _record(ticket, "--replace", "--ac", "AC1=tests/a.py::t")
        ticket.refresh_from_db()
        criteria = ticket.extra["spec_coverage"]["acceptance_criteria"]
        assert [ac["id"] for ac in criteria] == ["AC1"]

    def test_records_the_override_escape_hatch(self) -> None:
        ticket = self._ticket()
        result = _record(ticket, "--override-reason", "pure refactor, no acceptance criteria")
        ticket.refresh_from_db()
        assert ticket.extra["spec_coverage_override"] == {"reason": "pure refactor, no acceptance criteria"}
        assert result["override_reason"] == "pure refactor, no acceptance criteria"

    def test_empty_invocation_exits_nonzero_and_records_nothing(self) -> None:
        ticket = self._ticket()
        with pytest.raises(SystemExit):
            _record(ticket)
        ticket.refresh_from_db()
        assert ticket.extra == {}

    def test_malformed_ac_exits_nonzero_and_records_nothing(self) -> None:
        ticket = self._ticket()
        with pytest.raises(SystemExit):
            _record(ticket, "--ac", "AC1 has no equals sign")
        ticket.refresh_from_db()
        assert ticket.extra == {}

    def test_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "record-spec-coverage", "999999", "--ac", "AC1=tests/a.py::t")


class TestFlagIsSatisfiableEndToEnd(TestCase):
    """With the flag ON, delivery is blocked without a manifest and passes with one."""

    def _retrospected(self) -> Ticket:
        return Ticket.objects.create(overlay="acme", state=Ticket.State.RETROSPECTED)

    def test_no_manifest_blocks_delivery(self) -> None:
        ticket = self._retrospected()
        with _gate(required=True), pytest.raises(SpecCoverageDodError):
            ticket.mark_delivered()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETROSPECTED

    def test_recorded_coverage_delivers(self) -> None:
        ticket = self._retrospected()
        _record(ticket, "--ac", "AC1=tests/a.py::test_one", "--ac", "AC2=tests/b.py::test_two")
        ticket.refresh_from_db()
        with _gate(required=True):
            ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED

    def test_recorded_uncovered_ac_still_blocks_delivery(self) -> None:
        ticket = self._retrospected()
        _record(ticket, "--ac", "AC1=tests/a.py::test_one", "--ac", "AC2=")
        ticket.refresh_from_db()
        with _gate(required=True), pytest.raises(SpecCoverageDodError) as exc:
            ticket.mark_delivered()
        assert "AC2" in str(exc.value)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETROSPECTED

    def test_recorded_override_delivers(self) -> None:
        ticket = self._retrospected()
        _record(ticket, "--override-reason", "docs-only change")
        ticket.refresh_from_db()
        with _gate(required=True):
            ticket.mark_delivered()
        assert ticket.state == Ticket.State.DELIVERED
