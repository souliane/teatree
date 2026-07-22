"""Merged-ticket reconcile — the #3540 out-of-keystone-merge wedge escape.

An author-role ticket entered via a non-ladder phase (``debugging`` produces no
work-state) whose PR merged outside the keystone has NO automatic exit from its
entry state: both ``NOT_STARTED`` -> terminal hatches are reviewer-only, and
``reconcile_merged`` is only ever driven by the keystone. This sweep is the
missing driver — it fires ``reconcile_merged`` for any pre-merged ticket with a
merged ``PullRequest`` row, role- and phase-agnostic, guarded by
``merge_evidence`` so an untrustworthy row never advances.
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates import merge_evidence_gate
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket
from teatree.loop import merged_ticket_reconcile
from teatree.loop.merged_ticket_reconcile import reconcile_merged_tickets

_FORTY_HEX = "a" * 40


@contextlib.contextmanager
def _merge_evidence(*, required: bool) -> Iterator[None]:
    with patch.object(
        merge_evidence_gate,
        "get_effective_settings",
        return_value=UserSettings(require_merge_evidence=required),
    ):
        yield


def _merged_pr(ticket: Ticket) -> PullRequest:
    return PullRequest.objects.create(
        ticket=ticket,
        url=f"https://github.com/souliane/teatree/pull/{ticket.pk}",
        repo="souliane/teatree",
        iid=str(ticket.pk),
        overlay=ticket.overlay,
        state=PullRequest.State.MERGED,
    )


def _audit_for(ticket: Ticket) -> None:
    clear = MergeClear.objects.create(
        ticket=ticket,
        pr_id=ticket.pk,
        slug="souliane/teatree",
        reviewed_sha=_FORTY_HEX,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )
    MergeAudit.objects.create(clear=clear, merged_sha=_FORTY_HEX, required_checks_status="green")


class TestReconcileMergedTickets(TestCase):
    def test_author_not_started_with_merged_pr_reaches_merged(self) -> None:
        """The #3540 incident shape: author ticket at NOT_STARTED, PR merged -> MERGED."""
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket)

        assert reconcile_merged_tickets() == 1
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_advances_every_pre_merged_state(self) -> None:
        pre_merged = [
            Ticket.State.NOT_STARTED,
            Ticket.State.SCOPED,
            Ticket.State.STARTED,
            Ticket.State.PLANNED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.REVIEWED,
            Ticket.State.SHIPPED,
            Ticket.State.IN_REVIEW,
        ]
        for state in pre_merged:
            ticket = Ticket.objects.create(overlay="test", state=state)
            _merged_pr(ticket)

        assert reconcile_merged_tickets() == len(pre_merged)
        assert set(Ticket.objects.values_list("state", flat=True)) == {Ticket.State.MERGED}

    def test_ticket_without_a_merged_pr_is_left_alone(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        PullRequest.objects.create(
            ticket=ticket,
            url="https://github.com/souliane/teatree/pull/1",
            repo="souliane/teatree",
            iid="1",
            overlay="test",
            state=PullRequest.State.OPEN,
        )

        assert reconcile_merged_tickets() == 0
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_post_merged_states_are_never_dragged_backward(self) -> None:
        for state in (Ticket.State.MERGED, Ticket.State.RETROSPECTED, Ticket.State.DELIVERED, Ticket.State.IGNORED):
            ticket = Ticket.objects.create(overlay="test", state=state)
            _merged_pr(ticket)

        assert reconcile_merged_tickets() == 0
        assert Ticket.State.MERGED not in set(
            Ticket.objects.exclude(state=Ticket.State.MERGED).values_list("state", flat=True)
        )
        assert set(Ticket.objects.values_list("state", flat=True)) == {
            Ticket.State.MERGED,
            Ticket.State.RETROSPECTED,
            Ticket.State.DELIVERED,
            Ticket.State.IGNORED,
        }

    def test_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket)

        assert reconcile_merged_tickets() == 1
        assert reconcile_merged_tickets() == 0
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_gate_on_without_evidence_is_a_fail_closed_skip(self) -> None:
        """merge_evidence ON + no MergeAudit + forge cannot confirm -> ticket left stranded, no crash."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket)

        with (
            _merge_evidence(required=True),
            patch.object(merge_evidence_gate, "forge_confirms_merged", return_value=False),
        ):
            assert reconcile_merged_tickets() == 0
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_gate_on_with_merge_audit_advances(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.NOT_STARTED)
        _merged_pr(ticket)
        _audit_for(ticket)

        with _merge_evidence(required=True):
            assert reconcile_merged_tickets() == 1
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_one_poison_ticket_does_not_abort_the_sweep(self) -> None:
        good = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        _merged_pr(good)
        bad = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        _merged_pr(bad)

        real_one = merged_ticket_reconcile._reconcile_one

        def _boom(ticket: Ticket) -> bool:
            if ticket.pk == bad.pk:
                msg = "boom"
                raise RuntimeError(msg)
            return real_one(ticket)

        with patch.object(merged_ticket_reconcile, "_reconcile_one", _boom):
            assert reconcile_merged_tickets() == 1
        good.refresh_from_db()
        assert good.state == Ticket.State.MERGED
