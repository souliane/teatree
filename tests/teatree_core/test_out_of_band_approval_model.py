"""The attributable, single-use override for the phase-coverage gate (#3762).

Mirrors ``E2EBypassApproval`` (#1967) / ``OnBehalfApproval`` (#960): a durable
row written only through a guarded factory, refused for a maker/coding-agent/loop
approver, bound to one ticket at one reviewed tree, consumed once. The extra
field is the mandatory ``reason`` — an unattributed, unexplained bypass is
exactly the hole the gate closes.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.out_of_band_approval import OutOfBandWorkApproval, OutOfBandWorkApprovalError

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)


class TestRecordContract(TestCase):
    def test_records_an_attributable_approval(self) -> None:
        approval = OutOfBandWorkApproval.record(
            ticket=_ticket(), head_sha=_SHA.upper(), approver_id=" souliane ", reason="dependency bump"
        )
        assert (approval.head_sha, approval.approver_id, approval.reason) == (_SHA, "souliane", "dependency bump")
        assert approval.consumed_at is None

    def test_refuses_an_abbreviated_sha(self) -> None:
        with pytest.raises(OutOfBandWorkApprovalError, match="full 40-char hex commit SHA"):
            OutOfBandWorkApproval.record(ticket=_ticket(), head_sha="a" * 8, approver_id="souliane", reason="revert")

    def test_refuses_a_blank_approver(self) -> None:
        with pytest.raises(OutOfBandWorkApprovalError, match="approver_id is required"):
            OutOfBandWorkApproval.record(ticket=_ticket(), head_sha=_SHA, approver_id="  ", reason="revert")

    def test_refuses_a_maker_or_loop_approver(self) -> None:
        for approver in ("maker", "coding-agent", "merge-loop"):
            with pytest.raises(OutOfBandWorkApprovalError, match="maker/coding-agent/loop role"):
                OutOfBandWorkApproval.record(
                    ticket=_ticket(), head_sha=_SHA, approver_id=approver, reason="self-authorized"
                )

    def test_refuses_a_blank_reason(self) -> None:
        with pytest.raises(OutOfBandWorkApprovalError, match="reason is required"):
            OutOfBandWorkApproval.record(ticket=_ticket(), head_sha=_SHA, approver_id="souliane", reason="   ")


class TestConsume(TestCase):
    def test_consume_is_single_use_and_tree_scoped(self) -> None:
        ticket = _ticket()
        OutOfBandWorkApproval.record(ticket=ticket, head_sha=_SHA, approver_id="souliane", reason="docs typo")

        assert OutOfBandWorkApproval.has_unconsumed(ticket, _SHA)
        assert OutOfBandWorkApproval.consume(ticket, "b" * 40) is None

        consumed = OutOfBandWorkApproval.consume(ticket, _SHA)
        assert consumed is not None
        assert consumed.consumed_at is not None
        assert OutOfBandWorkApproval.consume(ticket, _SHA) is None
        assert not OutOfBandWorkApproval.has_unconsumed(ticket, _SHA)
