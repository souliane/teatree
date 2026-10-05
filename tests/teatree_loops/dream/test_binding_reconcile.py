"""Two conflicting BINDING memories queue ONE reconciliation gap for the sweep; the pass files no issue (#2723)."""

from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.dream_gap_ledger import pending_entries
from teatree.core.models.types import DreamGapEntry
from teatree.loops.dream.binding_reconcile import queue_binding_reconciliations
from teatree.loops.dream.merge import BindingConflict

UMBRELLA = "https://gitlab.com/o/factory/-/work_items/249"


def _conflict(survivor: str = "feedback_bind_one", absorbed: str = "feedback_bind_two") -> BindingConflict:
    return BindingConflict(
        survivor_name=survivor,
        absorbed_name=absorbed,
        survivor_path=Path(f"/m/{survivor}.md"),
        absorbed_path=Path(f"/m/{absorbed}.md"),
    )


class QueueBindingReconciliationsTestCase(TestCase):
    def setUp(self) -> None:
        self.umbrella = Ticket.objects.create(issue_url=UMBRELLA)

    def _pending(self) -> list[DreamGapEntry]:
        self.umbrella.refresh_from_db()
        return pending_entries(self.umbrella)

    def test_a_conflict_is_queued_as_a_gap_naming_both_files(self) -> None:
        outcomes = queue_binding_reconciliations(umbrella_url=UMBRELLA, conflicts=[_conflict()])

        assert [(o.filed, o.ticket_url) for o in outcomes] == [(True, UMBRELLA)]
        [entry] = self._pending()
        assert "reconcil" in entry["title"].lower()
        assert "feedback_bind_one.md" in entry["detail"]
        assert "feedback_bind_two.md" in entry["detail"]

    def test_the_same_pair_in_either_order_queues_once(self) -> None:
        queue_binding_reconciliations(umbrella_url=UMBRELLA, conflicts=[_conflict()])
        outcomes = queue_binding_reconciliations(
            umbrella_url=UMBRELLA, conflicts=[_conflict(survivor="feedback_bind_two", absorbed="feedback_bind_one")]
        )

        assert outcomes[0].filed is False
        assert len(self._pending()) == 1

    def test_a_banned_term_is_withheld(self) -> None:
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="customer-name"):
            outcomes = queue_binding_reconciliations(umbrella_url=UMBRELLA, conflicts=[_conflict()])

        assert outcomes[0].withheld is True
        assert self._pending() == []

    def test_a_dry_run_queues_nothing(self) -> None:
        assert queue_binding_reconciliations(umbrella_url=UMBRELLA, conflicts=[_conflict()], dry_run=True) == []
        assert self._pending() == []

    def test_with_no_umbrella_ticket_nothing_is_queued_and_nothing_is_filed(self) -> None:
        self.umbrella.delete()

        outcomes = queue_binding_reconciliations(umbrella_url=UMBRELLA, conflicts=[_conflict()])

        assert outcomes[0].filed is False
        assert UMBRELLA in outcomes[0].reason
        assert not Ticket.objects.exists()
