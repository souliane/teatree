"""``ticket clear`` enforces the mandatory-E2E gate (#1967).

The §17.4 per-diff CLEAR is the second gate site: a ticket-bound CLEAR for a
customer-display-impacting change is refused unless green E2E evidence exists at
the reviewed SHA, OR a single-use user bypass exists, OR the gate kill-switch is
off. A CLEAR with no resolved ticket (out-of-FSM) is not gated here — the gate
binds to a ticket's evidence.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import E2EBypassApproval, E2eMandatoryRun, Ticket, Worktree

_SHA = "9" * 40

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class _ImpactingOverlay:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return True


class _SafeOverlay:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return False


def _clear(ticket: Ticket) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "ticket",
            "clear",
            "7",
            "org/repo",
            reviewed_sha=_SHA,
            reviewer_identity="reviewer-bob",
            ticket_id=ticket.pk,
        ),
    )


class _ClearGateBase(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/40", overlay="t3-teatree")
        Worktree.objects.create(
            ticket=self.ticket, overlay="t3-teatree", repo_path="/tmp/x", branch="b", extra={"worktree_path": "/tmp/x"}
        )
        # Pin branch-currency + schema so only the E2E gate decides the outcome.
        patcher_currency = patch(
            "teatree.core.management.commands.ticket.check_clear_branch_currency", return_value=None
        )
        patcher_schema = patch("teatree.core.management.commands.ticket.require_current_schema", return_value=None)
        patcher_currency.start()
        patcher_schema.start()
        self.addCleanup(patcher_currency.stop)
        self.addCleanup(patcher_schema.stop)


class TestClearBlocks(_ClearGateBase):
    def test_impacting_no_evidence_blocks_clear(self) -> None:
        with patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            result = _clear(self.ticket)
        assert result["issued"] is False
        assert "record-e2e-run" in str(result["error"])
        assert "e2e-bypass" in str(result["error"])


class TestClearAllows(_ClearGateBase):
    def test_safe_overlay_allows_clear(self) -> None:
        with patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_SafeOverlay()):
            result = _clear(self.ticket)
        assert result["issued"] is True

    def test_green_posted_evidence_at_sha_allows_clear(self) -> None:
        E2eMandatoryRun.record(
            ticket=self.ticket,
            head_sha=_SHA,
            spec="x",
            result="green",
            posted_url="https://example.com/i/40#note_1",
        )
        with patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            result = _clear(self.ticket)
        assert result["issued"] is True

    def test_green_but_unposted_evidence_blocks_clear(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url="")
        with patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            result = _clear(self.ticket)
        assert result["issued"] is False
        assert "record-e2e-run" in str(result["error"])

    def test_recorded_bypass_allows_clear(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id="souliane")
        with patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            result = _clear(self.ticket)
        assert result["issued"] is True
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is False
