"""``lifecycle record-anti-vacuity`` records the SHA-bound attestation (#1829).

The command is the recording seam for the anti-vacuity gate: it stamps
``ticket.extra['anti_vacuity_attestation']`` (head SHA + AC-coverage + proven
tests, or the explicit no-new-tests claim) that the request-review / merge
transitions read. A partial record (missing head SHA, missing AC-coverage, or
neither a proven test nor the no-new-tests claim) is refused so it can never
satisfy the gate.
"""

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket

pytestmark = pytest.mark.django_db

_SHA = "a" * 40


class TestRecordAntiVacuityCommand(TestCase):
    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEWED)

    def test_records_attestation_with_proven_tests(self) -> None:
        ticket = self._ticket()
        out = call_command(
            "lifecycle",
            "record-anti-vacuity",
            str(ticket.pk),
            head_sha=_SHA,
            ac_coverage="AC1-3 mapped to test_foo / test_bar",
            proven_test=["tests/x.py::test_a", "tests/y.py::test_b"],
        )
        ticket.refresh_from_db()
        att = ticket.extra["anti_vacuity_attestation"]
        assert att["head_sha"] == _SHA
        assert att["proven_tests"] == ["tests/x.py::test_a", "tests/y.py::test_b"]
        assert "2 proven test(s)" in out

    def test_records_attestation_with_no_new_tests(self) -> None:
        ticket = self._ticket()
        out = call_command(
            "lifecycle",
            "record-anti-vacuity",
            str(ticket.pk),
            head_sha=_SHA,
            ac_coverage="docs-only diff; AC mapped",
            no_new_tests=True,
        )
        ticket.refresh_from_db()
        att = ticket.extra["anti_vacuity_attestation"]
        assert att["no_new_tests"] is True
        assert att["proven_tests"] == []
        assert "no new tests" in out

    def test_refused_without_head_sha(self) -> None:
        ticket = self._ticket()
        out = call_command(
            "lifecycle",
            "record-anti-vacuity",
            str(ticket.pk),
            ac_coverage="AC mapped",
            proven_test=["tests/x.py::test_a"],
        )
        ticket.refresh_from_db()
        assert "refused" in out
        assert "anti_vacuity_attestation" not in (ticket.extra or {})

    def test_refused_with_neither_proven_test_nor_no_new_tests(self) -> None:
        ticket = self._ticket()
        out = call_command(
            "lifecycle",
            "record-anti-vacuity",
            str(ticket.pk),
            head_sha=_SHA,
            ac_coverage="AC mapped",
        )
        ticket.refresh_from_db()
        assert "refused" in out
        assert "anti_vacuity_attestation" not in (ticket.extra or {})

    def test_refused_without_ac_coverage(self) -> None:
        ticket = self._ticket()
        out = call_command(
            "lifecycle",
            "record-anti-vacuity",
            str(ticket.pk),
            head_sha=_SHA,
            proven_test=["tests/x.py::test_a"],
        )
        ticket.refresh_from_db()
        assert "refused" in out
        assert "anti_vacuity_attestation" not in (ticket.extra or {})
