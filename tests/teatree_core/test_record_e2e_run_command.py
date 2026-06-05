"""``lifecycle record-e2e-run`` records SHA-bound, POSTED E2E evidence (#1967).

Records an ``E2eMandatoryRun`` for the ticket at the reviewed head SHA. A green
run satisfies the mandatory-E2E gate only when ``--posted-url`` is given — a
recorded-but-unposted run records provenance but does NOT satisfy the gate
(user directive: recorded evidence is not enough, it must be posted).
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import E2eMandatoryRun, Ticket

_SHA = "5" * 40
_URL = "https://example.com/i/50#note_3"

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class TestRecordE2ERunCommand(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/50", overlay="t3-teatree")

    def _run(self, *flags: str) -> dict[str, object]:
        with patch("teatree.core.management.commands.lifecycle.assert_lifecycle_db_is_canonical", return_value=None):
            return cast(
                "dict[str, object]",
                call_command("lifecycle", "record-e2e-run", str(self.ticket.pk), *flags),
            )

    def test_records_green_posted_run_satisfying_gate(self) -> None:
        result = self._run("--spec", "e2e/loan.spec.ts", "--result", "green", "--head-sha", _SHA, "--posted-url", _URL)
        assert result["recorded"] is True
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _SHA) is True

    def test_green_run_without_posted_url_does_not_satisfy_gate(self) -> None:
        result = self._run("--spec", "e2e/loan.spec.ts", "--result", "green", "--head-sha", _SHA)
        assert result["recorded"] is True
        # Recorded, but unposted -> gate not satisfied.
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _SHA) is False

    def test_records_red_run_without_satisfying_gate(self) -> None:
        self._run("--spec", "e2e/loan.spec.ts", "--result", "red", "--head-sha", _SHA, "--posted-url", _URL)
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _SHA) is False

    def test_refuses_missing_spec(self) -> None:
        result = self._run("--spec", "", "--result", "green", "--head-sha", _SHA, "--posted-url", _URL)
        assert result["recorded"] is False

    def test_refuses_bad_sha(self) -> None:
        result = self._run("--spec", "x", "--result", "green", "--head-sha", "abc", "--posted-url", _URL)
        assert result["recorded"] is False
