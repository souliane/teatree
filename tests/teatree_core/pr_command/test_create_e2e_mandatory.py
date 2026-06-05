"""``pr create`` enforces the mandatory-E2E gate (#1967).

The first gate site: a customer-display-impacting change is refused at PR
creation unless green E2E evidence exists at the reviewed tree (or a single-use
user bypass). Anti-vacuous: the same shippable ticket SHIPS when the change is
non-impacting and BLOCKS when it is impacting with no evidence — the classifier
verdict is what flips the outcome.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.models import E2eMandatoryRun, Ticket

from ._shared import _MOCK_OVERLAY, _shippable_ticket

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_SHA = "7" * 40


class _ImpactingOverlay:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return True


class TestPrCreateE2EMandatory(TestCase):
    def _create(self, ticket: Ticket) -> dict[str, object]:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()),
            patch("teatree.core.management.commands._ship_gates.git.head_sha", return_value=_SHA),
            patch(
                "teatree.core.management.commands._ship_gates.visual_qa.changed_files",
                return_value=["app/views.py"],
            ),
        ):
            return cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

    def test_blocks_impacting_change_without_evidence(self) -> None:
        ticket = _shippable_ticket()
        result = self._create(ticket)
        ticket.refresh_from_db()
        assert result.get("allowed") is False
        assert "record-e2e-run" in str(result.get("error"))
        assert "e2e-bypass" in str(result.get("error"))
        # FSM must NOT advance to SHIPPED on a block.
        assert ticket.state != Ticket.State.SHIPPED

    def test_allows_impacting_change_with_green_posted_evidence_at_sha(self) -> None:
        ticket = _shippable_ticket()
        E2eMandatoryRun.record(
            ticket=ticket,
            head_sha=_SHA,
            spec="e2e/x.spec.ts",
            result="green",
            posted_url="https://example.com/i#note_1",
        )
        result = self._create(ticket)
        ticket.refresh_from_db()
        assert result.get("allowed") is not False
        assert ticket.state == Ticket.State.SHIPPED

    def test_blocks_impacting_change_with_green_but_unposted_evidence(self) -> None:
        ticket = _shippable_ticket()
        E2eMandatoryRun.record(ticket=ticket, head_sha=_SHA, spec="e2e/x.spec.ts", result="green", posted_url="")
        result = self._create(ticket)
        ticket.refresh_from_db()
        assert result.get("allowed") is False
        assert ticket.state != Ticket.State.SHIPPED
