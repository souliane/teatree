"""Tests for teatree.core.gates.spec_coverage_gate — the per-ticket spec-coverage DoD gate.

The gate forecloses declaring a ticket *done* on a partial subset of its spec:
when ``require_spec_coverage`` is on, ``mark_delivered`` (RETROSPECTED →
DELIVERED) is refused unless every acceptance criterion the ticket carries in
``extra['spec_coverage']`` has at least one backing test. The pure helpers
(``spec_coverage_required``, ``acceptance_criteria``, ``uncovered_acs``,
``override_reason``, ``has_full_coverage``, ``check_spec_coverage``) are
exercised directly; the FSM wiring is exercised through ``Ticket.mark_delivered``
so a ticket with an uncovered AC cannot reach DELIVERED.

``require_spec_coverage`` is pinned per test by patching the gate's
``get_effective_settings`` rather than the host ``~/.teatree.toml`` so the suite
is deterministic.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates.spec_coverage_gate import (
    SpecCoverageDodError,
    acceptance_criteria,
    check_spec_coverage,
    has_full_coverage,
    override_reason,
    spec_coverage_required,
    uncovered_acs,
)
from teatree.core.models import Ticket

# A manifest where every declared AC carries at least one backing test.
_FULLY_COVERED = {
    "acceptance_criteria": [
        {
            "id": "AC1",
            "description": "the gate refuses delivery when an AC has no test",
            "tests": ["tests/teatree_core/test_spec_coverage_gate.py::test_uncovered_ac_cannot_deliver"],
        },
        {
            "id": "AC2",
            "description": "a fully-covered ticket delivers",
            "tests": ["tests/teatree_core/test_spec_coverage_gate.py::test_fully_covered_delivers"],
        },
    ],
}

# A manifest where AC2 is declared but carries no backing test.
_PARTIALLY_COVERED = {
    "acceptance_criteria": [
        {"id": "AC1", "description": "covered", "tests": ["tests/foo.py::test_a"]},
        {"id": "AC2", "description": "no test maps to this AC", "tests": []},
    ],
}


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.spec_coverage_gate.get_effective_settings",
        return_value=UserSettings(require_spec_coverage=required),
    ):
        yield


def _ticket(**extra: object) -> Ticket:
    return Ticket.objects.create(overlay="acme", extra=dict(extra))


class TestSpecCoverageRequired(TestCase):
    def test_off_by_default(self) -> None:
        with _gate(required=False):
            assert spec_coverage_required() is False

    def test_on_when_configured(self) -> None:
        with _gate(required=True):
            assert spec_coverage_required() is True


class TestAcceptanceCriteria(TestCase):
    def test_no_manifest_means_no_criteria(self) -> None:
        assert acceptance_criteria(_ticket()) == []

    def test_non_mapping_manifest_means_no_criteria(self) -> None:
        assert acceptance_criteria(_ticket(spec_coverage="not-a-dict")) == []

    def test_non_list_criteria_means_no_criteria(self) -> None:
        assert acceptance_criteria(_ticket(spec_coverage={"acceptance_criteria": "x"})) == []

    def test_criteria_are_returned(self) -> None:
        criteria = acceptance_criteria(_ticket(spec_coverage=_FULLY_COVERED))
        assert [ac["id"] for ac in criteria] == ["AC1", "AC2"]


class TestUncoveredAcs(TestCase):
    def test_fully_covered_has_no_uncovered(self) -> None:
        assert uncovered_acs(_ticket(spec_coverage=_FULLY_COVERED)) == []
        assert has_full_coverage(_ticket(spec_coverage=_FULLY_COVERED)) is True

    def test_ac_with_empty_tests_is_uncovered(self) -> None:
        assert uncovered_acs(_ticket(spec_coverage=_PARTIALLY_COVERED)) == ["AC2"]
        assert has_full_coverage(_ticket(spec_coverage=_PARTIALLY_COVERED)) is False

    def test_ac_with_blank_test_string_is_uncovered(self) -> None:
        manifest = {"acceptance_criteria": [{"id": "AC1", "description": "x", "tests": ["   "]}]}
        assert uncovered_acs(_ticket(spec_coverage=manifest)) == ["AC1"]

    def test_ac_with_no_tests_key_is_uncovered(self) -> None:
        manifest = {"acceptance_criteria": [{"id": "AC9", "description": "x"}]}
        assert uncovered_acs(_ticket(spec_coverage=manifest)) == ["AC9"]

    def test_ac_without_id_falls_back_to_description(self) -> None:
        manifest = {"acceptance_criteria": [{"description": "anonymous AC", "tests": []}]}
        assert uncovered_acs(_ticket(spec_coverage=manifest)) == ["anonymous AC"]

    def test_no_manifest_reports_nothing_uncovered(self) -> None:
        # No manifest => no declared ACs => nothing to be uncovered. The
        # REQUIRED-manifest semantics (an empty manifest is itself a block when
        # the gate is on) lives in check_spec_coverage, not here.
        assert uncovered_acs(_ticket()) == []


class TestOverrideReason(TestCase):
    def test_absent_override_is_empty(self) -> None:
        assert override_reason(_ticket()) == ""

    def test_recorded_reason_is_returned(self) -> None:
        ticket = _ticket(spec_coverage_override={"reason": "no formal ACs — pure refactor"})
        assert override_reason(ticket) == "no formal ACs — pure refactor"


class TestCheckSpecCoverage(TestCase):
    def test_noop_when_gate_off(self) -> None:
        with _gate(required=False):
            check_spec_coverage(_ticket(spec_coverage=_PARTIALLY_COVERED))  # does not raise

    def test_noop_when_gate_off_even_without_manifest(self) -> None:
        with _gate(required=False):
            check_spec_coverage(_ticket())

    def test_fully_covered_passes_when_on(self) -> None:
        with _gate(required=True):
            check_spec_coverage(_ticket(spec_coverage=_FULLY_COVERED))

    def test_override_passes_when_on(self) -> None:
        with _gate(required=True):
            check_spec_coverage(_ticket(spec_coverage_override={"reason": "exempt"}))

    def test_uncovered_ac_refused_when_on(self) -> None:
        with _gate(required=True), pytest.raises(SpecCoverageDodError):
            check_spec_coverage(_ticket(spec_coverage=_PARTIALLY_COVERED))

    def test_empty_manifest_refused_when_on(self) -> None:
        # The whole point of the gate: a ticket carrying NO spec_coverage manifest
        # cannot be declared done when the gate is on — that is the "partial
        # subset" the gate forecloses (zero ACs proven).
        with _gate(required=True), pytest.raises(SpecCoverageDodError):
            check_spec_coverage(_ticket())

    def test_refusal_names_the_uncovered_acs(self) -> None:
        with _gate(required=True), pytest.raises(SpecCoverageDodError) as exc:
            check_spec_coverage(_ticket(spec_coverage=_PARTIALLY_COVERED))
        assert "AC2" in str(exc.value)


class TestMarkDeliveredFsmGate(TestCase):
    def _retrospected(self, **extra: object) -> Ticket:
        return Ticket.objects.create(
            overlay="acme",
            state=Ticket.State.RETROSPECTED,
            extra=dict(extra),
        )

    def test_gate_off_delivers_regardless(self) -> None:
        with _gate(required=False):
            ticket = self._retrospected(spec_coverage=_PARTIALLY_COVERED)
            ticket.mark_delivered()
            assert ticket.state == Ticket.State.DELIVERED

    def test_fully_covered_delivers(self) -> None:
        with _gate(required=True):
            ticket = self._retrospected(spec_coverage=_FULLY_COVERED)
            ticket.mark_delivered()
            assert ticket.state == Ticket.State.DELIVERED

    def test_uncovered_ac_cannot_deliver(self) -> None:
        with _gate(required=True):
            ticket = self._retrospected(spec_coverage=_PARTIALLY_COVERED)
            with pytest.raises(SpecCoverageDodError):
                ticket.mark_delivered()
            ticket.refresh_from_db()
            assert ticket.state == Ticket.State.RETROSPECTED

    def test_override_delivers(self) -> None:
        with _gate(required=True):
            ticket = self._retrospected(spec_coverage_override={"reason": "exempt"})
            ticket.mark_delivered()
            assert ticket.state == Ticket.State.DELIVERED
