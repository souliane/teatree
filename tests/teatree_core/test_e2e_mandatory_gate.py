"""Mandatory-E2E gate over durable state (#1967).

The gate refuses a ship / §17.4 CLEAR for a customer-display-impacting change
unless recorded green E2E evidence exists at the reviewed tree OR a single-use
user-recorded bypass exists OR the gate's own kill-switch is off. The decision
is a pure function over durable rows + the classifier verdict + the SHA; no
network.

Symmetric corpus per the regression-eval rule. must-ALLOW: non-impacting diff;
green posted evidence; recorded bypass; kill-switch off. must-BLOCK: impacting
diff with no evidence and no bypass.

Each must-ALLOW is anti-vacuous: the same impacting diff with the satisfier
removed BLOCKs, so the satisfier is what flips the verdict.
"""

import pytest
from django.test import TestCase

from teatree.core.e2e_mandatory_gate import E2EMandatoryGateError, GateInputs, check_e2e_mandatory
from teatree.core.models import E2EBypassApproval, E2EBypassAudit, E2eMandatoryRun, Ticket

_SHA = "e" * 40
_USER = "souliane"
_URL = "https://example.com/issues/1#note_7"
# A glob set under which a serializer change is unknown (impacting), a test is not.
_NON_IMPACTING = ("*/tests/*", "test_*.py", "*.md")
_IMPACTING_DIFF = ["app/api/serializers.py"]
_NON_IMPACTING_DIFF = ["app/tests/test_api.py", "README.md"]


def _inputs(ticket: Ticket, *, diff: list[str], display_impacting: bool, kill_switch_on: bool = True) -> GateInputs:
    return GateInputs(
        ticket=ticket,
        changed_files=diff,
        head_sha=_SHA,
        display_impacting=display_impacting,
        gate_enabled=kill_switch_on,
    )


class TestMustBlock(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/20")

    def test_impacting_diff_no_evidence_no_bypass_blocks(self) -> None:
        with pytest.raises(E2EMandatoryGateError):
            check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))

    def test_block_message_names_both_remedies(self) -> None:
        with pytest.raises(E2EMandatoryGateError) as exc:
            check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))
        message = str(exc.value)
        assert "record-e2e-run" in message
        assert "e2e-bypass" in message


class TestMustAllow(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/21")

    def test_non_impacting_diff_allows(self) -> None:
        check_e2e_mandatory(_inputs(self.ticket, diff=_NON_IMPACTING_DIFF, display_impacting=False))

    def test_green_posted_evidence_allows(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="e2e/x.spec.ts", result="green", posted_url=_URL)
        check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))

    def test_recorded_bypass_allows_and_is_consumed(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))
        # Single-use: consumed, and an audit row written.
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is False
        assert E2EBypassAudit.objects.filter(ticket=self.ticket).count() == 1

    def test_kill_switch_off_allows(self) -> None:
        check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True, kill_switch_on=False))


class TestAntiVacuity(TestCase):
    """Each satisfier flips the verdict: remove it and the same diff BLOCKs."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/22")

    def test_green_evidence_at_wrong_sha_does_not_allow(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha="f" * 40, spec="x", result="green", posted_url=_URL)
        with pytest.raises(E2EMandatoryGateError):
            check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))

    def test_red_evidence_does_not_allow(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="red", posted_url=_URL)
        with pytest.raises(E2EMandatoryGateError):
            check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))

    def test_green_but_unposted_evidence_does_not_allow(self) -> None:
        # Recorded green run with NO posted comment URL — the gate stays blocked
        # (#1967: recorded evidence is not enough, it must be posted).
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url="")
        with pytest.raises(E2EMandatoryGateError):
            check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))

    def test_bypass_for_other_ticket_does_not_allow(self) -> None:
        other = Ticket.objects.create(issue_url="https://example.com/i/23")
        E2EBypassApproval.record(ticket=other, head_sha=_SHA, approver_id=_USER)
        with pytest.raises(E2EMandatoryGateError):
            check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))

    def test_consumed_bypass_does_not_allow_again(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))
        # A second gate evaluation at the same SHA has no unconsumed bypass left.
        with pytest.raises(E2EMandatoryGateError):
            check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))


class TestNeverLockout(TestCase):
    """The gate is satisfiable, never a hard trap (#1967).

    Three independent escapes each unblock a genuinely-impacting change without
    code: recording evidence, the user bypass, and the kill-switch. This is the
    CLI analogue of the never-lockout contract for hook gates — proven by the
    must-ALLOW corpus above, asserted here as one explicit statement so the
    property is named, not incidental.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/25")
        self.blocked = _inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True)

    def test_blocked_without_any_escape(self) -> None:
        with pytest.raises(E2EMandatoryGateError):
            check_e2e_mandatory(self.blocked)

    def test_posted_evidence_escape_unblocks(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url=_URL)
        check_e2e_mandatory(self.blocked)

    def test_user_bypass_escape_unblocks(self) -> None:
        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        check_e2e_mandatory(self.blocked)

    def test_kill_switch_escape_unblocks(self) -> None:
        check_e2e_mandatory(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True, kill_switch_on=False))


class TestNonConsumingPeek(TestCase):
    """``e2e_mandatory_block_message`` reports the verdict without consuming."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/24")

    def test_peek_returns_message_when_blocked(self) -> None:
        from teatree.core.e2e_mandatory_gate import e2e_mandatory_block_message  # noqa: PLC0415

        message = e2e_mandatory_block_message(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))
        assert "record-e2e-run" in message

    def test_peek_does_not_consume_bypass(self) -> None:
        from teatree.core.e2e_mandatory_gate import e2e_mandatory_block_message  # noqa: PLC0415

        E2EBypassApproval.record(ticket=self.ticket, head_sha=_SHA, approver_id=_USER)
        message = e2e_mandatory_block_message(_inputs(self.ticket, diff=_IMPACTING_DIFF, display_impacting=True))
        assert message == ""
        # Peek must NOT have consumed it.
        assert E2EBypassApproval.has_unconsumed(self.ticket, _SHA) is True
        assert E2EBypassAudit.objects.filter(ticket=self.ticket).count() == 0
