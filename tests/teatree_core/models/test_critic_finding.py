"""CriticFinding (SELFCATCH-5): the durable per-rubric-item finding record.

The row is upserted on ``(ticket, transition, rubric_item)`` so a re-run of the
critic overwrites the prior verdict rather than stacking duplicates, and the
unique constraint enforces exactly one row per (ticket, transition, item).
"""

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import CriticFinding, CriticFindingSpec, Ticket


def _spec(rubric_item: str, detail: str, **kwargs: str) -> CriticFindingSpec:
    return CriticFindingSpec(rubric_item=rubric_item, detail=detail, **kwargs)


class TestCriticFindingRecord(TestCase):
    def test_record_creates_a_fail_finding(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        row = CriticFinding.record(
            ticket=ticket,
            transition="mark_delivered",
            spec=_spec("done_not_done", "no merged-SHA evidence", adversarial_question="Is this actually done?"),
        )
        assert row.status == CriticFinding.Status.FAIL
        assert row.detail == "no merged-SHA evidence"
        assert row.adversarial_question == "Is this actually done?"

    def test_record_upserts_the_same_key(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        first = CriticFinding.record(ticket=ticket, transition="mark_delivered", spec=_spec("done_not_done", "first"))
        second = CriticFinding.record(ticket=ticket, transition="mark_delivered", spec=_spec("done_not_done", "second"))
        assert first.pk == second.pk
        assert CriticFinding.objects.filter(ticket=ticket, rubric_item="done_not_done").count() == 1
        assert CriticFinding.objects.get(pk=first.pk).detail == "second"

    def test_record_can_mark_instrumentation_gap(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        row = CriticFinding.record(
            ticket=ticket,
            transition="mark_delivered",
            spec=_spec("coherence", "predicate raised", status=CriticFinding.Status.INSTRUMENTATION_GAP),
        )
        assert row.status == CriticFinding.Status.INSTRUMENTATION_GAP

    def test_unique_constraint_blocks_a_manual_duplicate(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        CriticFinding.objects.create(ticket=ticket, transition="mark_delivered", rubric_item="deferred", detail="a")
        with pytest.raises(IntegrityError), transaction.atomic():
            CriticFinding.objects.create(ticket=ticket, transition="mark_delivered", rubric_item="deferred", detail="b")

    def test_distinct_items_coexist(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED)
        CriticFinding.record(ticket=ticket, transition="mark_delivered", spec=_spec("deferred", "a"))
        CriticFinding.record(ticket=ticket, transition="mark_delivered", spec=_spec("coherence", "b"))
        assert CriticFinding.objects.filter(ticket=ticket).count() == 2
