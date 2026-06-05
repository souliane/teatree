"""Anti-vacuity attestation gate (#1829).

Golden must-ALLOW / must-DENY corpus pinning both directions of the gate: the
bypass dimension — a vacuous / missing / stale-SHA attestation slips through
(must DENY) — and the over-block dimension — a legitimately attested MR bound to
the current head is never wrongly blocked (must ALLOW).

The gate reads durable ``ticket.extra['anti_vacuity_attestation']`` plus the
live head SHA. ``require_anti_vacuity_attestation`` is pinned per test rather
than the host ``~/.teatree.toml`` so the suite is deterministic.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.anti_vacuity_gate import (
    AntiVacuityAttestationError,
    anti_vacuity_required,
    check_anti_vacuity_attestation,
    is_bound_to,
    is_complete,
    recorded_attestation,
)
from teatree.core.models import Ticket

pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.anti_vacuity_gate.get_effective_settings",
        return_value=UserSettings(require_anti_vacuity_attestation=required),
    ):
        yield


def _attested_ticket(
    *,
    head_sha: str = _SHA,
    ac_coverage: str = "AC1-3 mapped to test_foo / test_bar; AC4 n/a",
    proven_tests: list[str] | None = None,
    no_new_tests: bool = False,
) -> Ticket:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEWED)
    ticket.record_anti_vacuity_attestation(
        head_sha,
        ac_coverage,
        ["tests/core/test_claim.py::test_lost_update"] if proven_tests is None else proven_tests,
        no_new_tests=no_new_tests,
    )
    ticket.refresh_from_db()
    return ticket


class TestGateAllows(TestCase):
    def test_complete_attestation_bound_to_current_head_passes(self) -> None:
        ticket = _attested_ticket(head_sha=_SHA)
        with _gate(required=True):
            check_anti_vacuity_attestation(ticket, _SHA, transition="merge")

    def test_no_new_tests_claim_with_ac_coverage_passes(self) -> None:
        ticket = _attested_ticket(head_sha=_SHA, proven_tests=[], no_new_tests=True)
        with _gate(required=True):
            check_anti_vacuity_attestation(ticket, _SHA, transition="request review")

    def test_mixed_case_head_sha_still_binds(self) -> None:
        ticket = _attested_ticket(head_sha=_SHA)
        with _gate(required=True):
            check_anti_vacuity_attestation(ticket, _SHA.upper(), transition="merge")

    def test_noop_when_gate_off_even_without_attestation(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEWED)
        with _gate(required=False):
            check_anti_vacuity_attestation(ticket, _SHA, transition="merge")


class TestGateDenies(TestCase):
    def test_no_attestation_is_blocked(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEWED)
        with _gate(required=True), pytest.raises(AntiVacuityAttestationError, match="no anti-vacuity attestation"):
            check_anti_vacuity_attestation(ticket, _SHA, transition="merge")

    def test_empty_proven_tests_without_no_new_tests_is_blocked(self) -> None:
        ticket = _attested_ticket(head_sha=_SHA, proven_tests=[], no_new_tests=False)
        with _gate(required=True), pytest.raises(AntiVacuityAttestationError, match="incomplete"):
            check_anti_vacuity_attestation(ticket, _SHA, transition="request review")

    def test_missing_ac_coverage_is_blocked(self) -> None:
        ticket = _attested_ticket(head_sha=_SHA, ac_coverage="   ")
        with _gate(required=True), pytest.raises(AntiVacuityAttestationError, match="incomplete"):
            check_anti_vacuity_attestation(ticket, _SHA, transition="merge")

    def test_stale_sha_attestation_is_blocked(self) -> None:
        ticket = _attested_ticket(head_sha=_OTHER_SHA)
        with _gate(required=True), pytest.raises(AntiVacuityAttestationError, match="stale"):
            check_anti_vacuity_attestation(ticket, _SHA, transition="merge")


class TestPredicates(TestCase):
    def test_is_complete_requires_ac_and_proof_or_no_new_tests(self) -> None:
        assert is_complete({"ac_coverage": "x", "proven_tests": ["t"]})
        assert is_complete({"ac_coverage": "x", "proven_tests": [], "no_new_tests": True})
        assert not is_complete({"ac_coverage": "x", "proven_tests": []})
        assert not is_complete({"ac_coverage": "", "proven_tests": ["t"]})
        assert not is_complete({})

    def test_is_bound_to_is_case_insensitive_and_rejects_empty(self) -> None:
        assert is_bound_to({"head_sha": _SHA}, _SHA.upper())
        assert not is_bound_to({"head_sha": _SHA}, _OTHER_SHA)
        assert not is_bound_to({"head_sha": ""}, _SHA)
        assert not is_bound_to({"head_sha": _SHA}, "")

    def test_recorded_attestation_empty_without_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEWED)
        assert recorded_attestation(ticket) == {}

    def test_anti_vacuity_required_reads_effective_settings(self) -> None:
        with patch(
            "teatree.core.anti_vacuity_gate.get_effective_settings",
            return_value=UserSettings(require_anti_vacuity_attestation=True),
        ):
            assert anti_vacuity_required() is True


class TestRecordAttestation(TestCase):
    def test_stores_normalized_sha_and_fields_on_extra(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEWED)
        ticket.record_anti_vacuity_attestation(_SHA.upper(), "AC mapped", ["tests/x.py::test_y"], no_new_tests=False)
        ticket.refresh_from_db()
        att = ticket.extra["anti_vacuity_attestation"]
        assert att["head_sha"] == _SHA
        assert att["ac_coverage"] == "AC mapped"
        assert att["proven_tests"] == ["tests/x.py::test_y"]
        assert att["no_new_tests"] is False
        assert att["at"].endswith("+00:00") or att["at"].endswith("Z")
