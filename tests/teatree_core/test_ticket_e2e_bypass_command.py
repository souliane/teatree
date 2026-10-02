"""``ticket e2e-bypass`` records a single-use mandatory-E2E bypass (#1967).

The user-only satisfier for the mandatory-E2E gate. It records an
``E2EBypassApproval`` scoped to the ticket + reviewed head SHA; a
maker/coding-agent/loop approver is refused (maker≠checker). The next gate
evaluation at that SHA consumes it once.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import E2EBypassApproval, Ticket

_SHA = "8" * 40

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class TestE2EBypassCommand(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/60", overlay="t3-teatree")

    def _run(self, *flags: str) -> dict[str, object]:
        with patch("teatree.core.management.commands.ticket.Command._resolve_ticket", return_value=self.ticket):
            return cast(
                "dict[str, object]",
                call_command("ticket", "e2e-bypass", str(self.ticket.pk), *flags),
            )

    def test_records_bypass(self) -> None:
        result = self._run("--approver", "souliane", "--head-sha", _SHA)
        assert result["recorded"] is True
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is True

    def test_refuses_maker_approver(self) -> None:
        result = self._run("--approver", "merge-loop", "--head-sha", _SHA)
        assert result["recorded"] is False
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is False

    def test_refuses_bad_sha(self) -> None:
        result = self._run("--approver", "souliane", "--head-sha", "nope")
        assert result["recorded"] is False
