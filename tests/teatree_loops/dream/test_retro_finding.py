"""Retro's synchronous entry into the gap drain (the ledger, not a memory file).

A retro finding is recorded as a VERIFIED core-gap ``ConsolidatedMemory`` row and
queued on the umbrella host's pending ledger for the backlog sweep. These tests drive that with
real rows — no LLM, no live forge.

The queueing is the ``memory_promote`` mechanism whoever fires it,
so every promoting case here opts in explicitly; the OFF case is its own class.
"""

import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loops.dream.destination import points_at_core_fix
from teatree.loops.dream.retro_finding import finding_cluster_key, promote_finding, record_finding
from teatree.loops.dream.umbrella_ledger import is_promotion_anchor

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"

_RULE = "Run the tree-wide health gate before any push."
_CITATION = "pushed without running the gate, CI went red"
_DESTINATION = "skills/ship/SKILL.md"


def _record() -> ConsolidatedMemory:
    return record_finding(rule=_RULE, citation=_CITATION, destination=_DESTINATION)


class FindingClusterKeyTestCase(TestCase):
    """Cluster identity is over the normalized rule, never the prose as typed."""

    def test_whitespace_and_case_do_not_change_the_key(self) -> None:
        assert finding_cluster_key("Run  the\n gate.") == finding_cluster_key("run the gate.")

    def test_a_different_rule_is_a_different_finding(self) -> None:
        assert finding_cluster_key("Run the gate.") != finding_cluster_key("Read the logs.")

    def test_the_key_fits_the_ledger_column(self) -> None:
        field = ConsolidatedMemory._meta.get_field("cluster_key")
        assert len(finding_cluster_key(_RULE)) <= field.max_length


class RecordFindingTestCase(TestCase):
    def test_a_finding_is_recorded_verified_and_classified_a_core_gap(self) -> None:
        row = _record()
        assert row.status == ConsolidatedMemory.Status.VERIFIED
        assert row.verified_citation == _CITATION
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET

    def test_recording_the_same_rule_twice_yields_one_row(self) -> None:
        assert _record().pk == _record().pk
        assert ConsolidatedMemory.objects.count() == 1

    def test_an_uncited_finding_is_refused_and_records_nothing(self) -> None:
        with pytest.raises(ValueError, match="citation"):
            record_finding(rule=_RULE, citation="   ", destination=_DESTINATION)
        assert ConsolidatedMemory.objects.count() == 0

    def test_the_destination_makes_pass_two_read_the_row_as_a_core_gap(self) -> None:
        # No LLM in the path: the destination hint alone drives Pass-2's own classifier.
        assert points_at_core_fix(_record().durable_destination) is True

    def test_a_destination_outside_a_teatree_fix_path_is_refused_and_records_nothing(self) -> None:
        with pytest.raises(ValueError, match="destination"):
            record_finding(rule=_RULE, citation=_CITATION, destination="~/notes/lessons.md")
        assert ConsolidatedMemory.objects.count() == 0

    def test_a_recorded_row_sits_in_the_drain_queue_a_later_pass_reads(self) -> None:
        row = _record()
        assert list(ConsolidatedMemory.objects.needs_ticket()) == [row]


def _pending() -> list[dict[str, str]]:
    return Ticket.objects.get(issue_url=UMBRELLA).extra.get("dream_gap_pending", [])


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "1"})
class PromoteFindingTestCase(TestCase):
    def setUp(self) -> None:
        Ticket.objects.create(issue_url=UMBRELLA)

    def test_a_finding_is_queued_once_and_schedules_nothing(self) -> None:
        outcome = promote_finding(rule=_RULE, umbrella_url=UMBRELLA)
        assert outcome.queued is True
        assert [entry["gap_key"] for entry in _pending()] == [finding_cluster_key(_RULE)]
        assert Task.objects.count() == 0

    def test_promoting_the_same_rule_twice_queues_nothing_the_second_time(self) -> None:
        promote_finding(rule=_RULE, umbrella_url=UMBRELLA)
        outcome = promote_finding(rule=_RULE, umbrella_url=UMBRELLA)
        assert outcome.queued is False
        assert len(_pending()) == 1

    def test_promotion_stamps_the_row_with_its_batch_anchor_so_it_leaves_the_queue(self) -> None:
        row = _record()
        promote_finding(rule=_RULE, umbrella_url=UMBRELLA)
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert is_promotion_anchor(row.ticket_url)
        assert list(ConsolidatedMemory.objects.needs_ticket()) == []

    def test_a_dry_run_queues_nothing(self) -> None:
        outcome = promote_finding(rule=_RULE, umbrella_url=UMBRELLA, dry_run=True)
        assert outcome.queued is False
        assert _pending() == []


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "0"})
class PromotionToggleTestCase(TestCase):
    """Queueing for the sweep is the ``memory_promote`` mechanism, however it is fired."""

    def test_the_toggle_off_records_the_gap_and_queues_nothing(self) -> None:
        Ticket.objects.create(issue_url=UMBRELLA)
        row = _record()
        outcome = promote_finding(rule=_RULE, umbrella_url=UMBRELLA)
        assert outcome.deferred is True
        assert _pending() == []
        assert list(ConsolidatedMemory.objects.needs_ticket()) == [row]
