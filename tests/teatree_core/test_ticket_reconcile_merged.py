"""``Ticket.reconcile_merged`` — state-complete FSM catch-up for PR-merge (#1343).

When the keystone merges a PR (``merge.execution.record_merge_and_advance``),
the linked ticket's FSM must advance to ``MERGED`` regardless of which
pre-merge state it sat in. The original guard only fired ``mark_merged()``
when the ticket was already at ``IN_REVIEW``/``MERGED``, so a ticket whose
PR landed while the FSM still read ``STARTED`` (a common shape when the
coding agent's session ended before the FSM advanced past coding) stayed
visibly stuck at ``started`` on the statusline forever.

This transition is the FSM-level expression of "PR merged ⇒ ticket merged":
any non-past-merged state -> MERGED. Mirrors ``reconcile_reviewed`` (#808).
"""

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.models import Ticket


class TestReconcileMerged(TestCase):
    def test_started_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_not_started_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_scoped_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SCOPED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_coded_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_tested_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.TESTED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_reviewed_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_shipped_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SHIPPED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_in_review_reconciles_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_merged_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        ticket.reconcile_merged()
        ticket.save()
        assert ticket.state == Ticket.State.MERGED

    def test_retrospected_cannot_reconcile_backwards(self) -> None:
        """A ticket past MERGED (RETROSPECTED) stays where it is.

        Mirrors the existing ``record_advance_skips_mark_merged`` invariant —
        the post-merge hook must never drag a ticket BACK from a post-MERGED
        state to MERGED.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.RETROSPECTED)
        with pytest.raises(TransitionNotAllowed):
            ticket.reconcile_merged()

    def test_delivered_cannot_reconcile_backwards(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.DELIVERED)
        with pytest.raises(TransitionNotAllowed):
            ticket.reconcile_merged()

    def test_ignored_cannot_reconcile_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IGNORED)
        with pytest.raises(TransitionNotAllowed):
            ticket.reconcile_merged()

    def test_source_set_partition_is_exhaustive(self) -> None:
        """Every state is either a reconcile_merged source or refused.

        Structural guard mirroring ``test_source_set_is_complete_partition_of_all_states``
        on ``reconcile_reviewed``: a future-added State member must be
        consciously classified here, not silently dropped.
        """
        all_states = set(Ticket.State)
        merge_source = set(Ticket._MERGED_RECONCILE_SOURCE_STATES)
        refused = {Ticket.State.RETROSPECTED, Ticket.State.DELIVERED, Ticket.State.IGNORED}
        assert merge_source.isdisjoint(refused), "a state is both reconcile-merged and refused"
        assert merge_source | refused == all_states, (
            f"State partition incomplete — unclassified: {all_states - merge_source - refused}"
        )
