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
from teatree.core.management.commands._spec_coverage_commands import (
    SpecCoverageCommands,
    SpecCoverageResult,
    parse_ac_specs,
)
from teatree.core.models import Ticket
from teatree.core.models.types import ac_label, spec_coverage_criteria


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.spec_coverage_gate.get_effective_settings",
        return_value=UserSettings(require_spec_coverage=required),
    ):
        yield


def _record(ticket: Ticket, *args: str) -> SpecCoverageResult:
    return cast("SpecCoverageResult", call_command("ticket", "record-spec-coverage", str(ticket.pk), *args))


def test_the_command_mounts_on_the_ticket_command_via_the_mixin() -> None:
    from teatree.core.management.commands.ticket import Command  # noqa: PLC0415 — deferred: needs the app registry

    assert issubclass(Command, SpecCoverageCommands)


class TestParseAcSpecs:
    def test_splits_label_from_its_comma_separated_tests(self) -> None:
        assert parse_ac_specs([" AC1 = tests/a.py::t1 , tests/a.py::t2 "]) == [
            {"id": "AC1", "tests": ["tests/a.py::t1", "tests/a.py::t2"]}
        ]

    def test_bare_label_records_a_declared_but_uncovered_ac(self) -> None:
        assert parse_ac_specs(["AC3="]) == [{"id": "AC3", "tests": []}]

    def test_missing_separator_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be"):
            parse_ac_specs(["AC1 tests/a.py::t"])

    def test_blank_label_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be"):
            parse_ac_specs(["  =tests/a.py::t"])


class TestSharedManifestShape:
    """``ac_label`` / ``spec_coverage_criteria`` are the one parse reader and writer share."""

    def test_label_prefers_id_then_description(self) -> None:
        assert ac_label({"id": "AC1", "description": "ignored"}) == "AC1"
        assert ac_label({"description": " anonymous AC "}) == "anonymous AC"
        assert ac_label({}) == "<unnamed-ac>"

    def test_criteria_drop_every_non_mapping_shape(self) -> None:
        assert spec_coverage_criteria(None) == []
        assert spec_coverage_criteria({"spec_coverage": "not-a-dict"}) == []
        assert spec_coverage_criteria({"spec_coverage": {"acceptance_criteria": "x"}}) == []
        assert spec_coverage_criteria({"spec_coverage": {"acceptance_criteria": [{"id": "AC1"}, "junk"]}}) == [
            {"id": "AC1"}
        ]


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
