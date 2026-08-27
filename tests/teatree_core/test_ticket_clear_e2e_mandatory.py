"""``ticket clear`` enforces the mandatory-E2E gate (#1967).

The §17.4 per-diff CLEAR is the second gate site: a ticket-bound CLEAR for a
customer-display-impacting change is refused unless green E2E evidence exists at
the reviewed SHA, OR a single-use user bypass exists, OR the gate kill-switch is
off. A CLEAR with no resolved ticket (out-of-FSM) is not gated here — the gate
binds to a ticket's evidence.
"""

from io import StringIO
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


class _ImpactingReview:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return True


class _ImpactingOverlay:
    review = _ImpactingReview()


class _SafeReview:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return False


class _SafeOverlay:
    review = _SafeReview()


def _clear(ticket: Ticket, **extra: object) -> dict[str, object]:
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
            # The slug is bare and the fixture's issue_url names no forge host, so the
            # resolver has nothing to derive from; these tests are about the E2E gate,
            # and naming the forge is what lets them reach it.
            forge="github",
            **extra,
        ),
    )


def _refused_clear(ticket: Ticket) -> str:
    """The stderr of a gate-blocked ``ticket clear``, asserting the nonzero exit (#932)."""
    err = StringIO()
    with pytest.raises(SystemExit) as exc:
        _clear(ticket, stderr=err)
    assert exc.value.code == 1
    return err.getvalue()


class _ClearGateBase(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/40", overlay="t3-teatree")
        Worktree.objects.create(
            ticket=self.ticket, overlay="t3-teatree", repo_path="/tmp/x", branch="b", extra={"worktree_path": "/tmp/x"}
        )
        # Pin the sibling pre-issue gates + schema so only the E2E gate decides.
        patcher_currency = patch(
            "teatree.core.management.commands._clear_preflight.check_clear_branch_currency", return_value=None
        )
        patcher_fork = patch(
            "teatree.core.management.commands._clear_preflight.check_clear_migration_fork", return_value=None
        )
        patcher_schema = patch("teatree.core.management.commands.ticket.require_current_schema", return_value=None)
        # ``org/repo`` is not a repo this machine's registry names, so issuance runs the
        # #1335 cross-repo reconcile — a LIVE forge read (nine ``gh`` calls) from a gate
        # unit test. Pin it to the identity the fixture's own slug already asserts.
        patcher_reconcile = patch(
            "teatree.core.merge.pr_slug_resolution._reconcile_slug_against_reviewed_sha",
            side_effect=lambda **kwargs: str(kwargs["initial_slug"]),
        )
        patcher_currency.start()
        patcher_fork.start()
        patcher_schema.start()
        patcher_reconcile.start()
        self.addCleanup(patcher_currency.stop)
        self.addCleanup(patcher_fork.stop)
        self.addCleanup(patcher_schema.stop)
        self.addCleanup(patcher_reconcile.stop)


class TestClearBlocks(_ClearGateBase):
    def test_impacting_no_evidence_blocks_clear(self) -> None:
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            refusal = _refused_clear(self.ticket)
        assert "record-e2e-run" in refusal
        assert "e2e-bypass" in refusal


class TestClearAllows(_ClearGateBase):
    def test_safe_overlay_allows_clear(self) -> None:
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_SafeOverlay()):
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
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            result = _clear(self.ticket)
        assert result["issued"] is True

    def test_green_but_unposted_evidence_blocks_clear(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url="")
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            refusal = _refused_clear(self.ticket)
        assert "record-e2e-run" in refusal

    def test_recorded_bypass_allows_clear(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id="souliane")
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            result = _clear(self.ticket)
        assert result["issued"] is True
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is False
