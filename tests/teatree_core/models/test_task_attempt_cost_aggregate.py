"""``cost_breakdown`` aggregates in SQL, not by instantiating every attempt (#3856 sibling).

The cycle-to-date spend chip on ``/dash/health/`` used to walk the whole
``TaskAttempt`` table through the ORM — 340k rows, doubled by a ``select_related``
join — and total it in Python, which is the 19s the operator saw. Every accumulator
in the breakdown is linear in the per-row token counts, so the same numbers fall out
of one ``GROUP BY`` over the small ``(model, lane, phase, estimated, reported)``
key space. These tests pin BOTH halves of that claim: the numbers are identical to
the per-row path, and the work is bounded by the number of distinct keys.
"""

from itertools import product

import pytest
from django.test import TestCase

from teatree.core.cost import CostBreakdown
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from tests.factories import TaskAttemptFactory, TaskFactory, TicketFactory

_MODELS = ("claude-opus-4-8", "sonnet", "haiku", "deepseek/v3", "")
_LANES = (TaskAttempt.Lane.SUBSCRIPTION, TaskAttempt.Lane.METERED, "")
_PHASES = ("coding", "reviewing", "")
#: Every combination of the five costing-key axes, so no axis is a proxy for another.
#: ``reported`` and ``estimated`` in particular must vary independently — the metered
#: router reports a cost that IS an estimate, so a corpus where "estimated" implies
#: "unreported" cannot tell a correct group key from one missing the reported axis.
_AXES = tuple(product(_MODELS, _LANES, _PHASES, (True, False), (True, False)))


def _seed_corpus() -> None:
    """One attempt per costing-key combination, with null token fields interleaved."""
    ticket = TicketFactory(state=Ticket.State.STARTED)
    tasks = {phase: TaskFactory(ticket=ticket, phase=phase) for phase in _PHASES}
    for index, (model, lane, phase, reported, estimated) in enumerate(_AXES):
        TaskAttemptFactory(
            task=tasks[phase],
            model=model,
            lane=lane,
            cost_usd=round(0.01 * (index + 1), 4) if reported else None,
            cost_is_estimated=estimated,
            input_tokens=None if index % 7 == 0 else 1000 + index,
            output_tokens=None if index % 11 == 0 else 200 + index,
            cache_read_tokens=5000 + index,
            cache_write_tokens=None if index % 5 == 0 else 300 + index,
        )


class TestSqlAggregateMatchesThePerRowPath(TestCase):
    """The SQL group-by must produce the byte-for-byte breakdown the per-row walk did."""

    @classmethod
    def setUpTestData(cls) -> None:
        _seed_corpus()

    def test_every_field_of_the_breakdown_matches(self) -> None:
        per_row = CostBreakdown.from_usages(TaskAttempt.objects.usages())
        aggregated = TaskAttempt.objects.cost_breakdown()

        assert aggregated.attempts == per_row.attempts
        assert aggregated.total_usd == pytest.approx(per_row.total_usd)
        assert aggregated.estimated_usd == pytest.approx(per_row.estimated_usd)
        assert aggregated.effective_tokens_total == pytest.approx(per_row.effective_tokens_total)
        for label, got, want in (
            ("per_tier_usd", aggregated.per_tier_usd, per_row.per_tier_usd),
            ("per_lane_usd", aggregated.per_lane_usd, per_row.per_lane_usd),
            ("per_phase_usd", aggregated.per_phase_usd, per_row.per_phase_usd),
            ("per_lane_effective_tokens", aggregated.per_lane_effective_tokens, per_row.per_lane_effective_tokens),
            ("per_lane_cache_hit_ratio", aggregated.per_lane_cache_hit_ratio, per_row.per_lane_cache_hit_ratio),
            ("per_phase_cache_hit_ratio", aggregated.per_phase_cache_hit_ratio, per_row.per_phase_cache_hit_ratio),
        ):
            assert sorted(got) == sorted(want), label
            for key in got:
                assert got[key] == pytest.approx(want[key]), f"{label}[{key}]"

    def test_an_empty_queryset_is_the_zero_breakdown(self) -> None:
        assert TaskAttempt.objects.none().cost_breakdown() == CostBreakdown()


class TestAggregationIsBoundedByDistinctKeys(TestCase):
    """The work must scale with the number of distinct buckets, never with row count."""

    @staticmethod
    def _seed_identical(count: int) -> None:
        ticket = TicketFactory(state=Ticket.State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        for _ in range(count):
            TaskAttemptFactory(
                task=task,
                model="claude-opus-4-8",
                lane=TaskAttempt.Lane.SUBSCRIPTION,
                cost_usd=0.5,
                cost_is_estimated=False,
                input_tokens=10,
                output_tokens=20,
                cache_read_tokens=30,
                cache_write_tokens=40,
            )

    def test_two_hundred_identical_attempts_collapse_to_one_group(self) -> None:
        self._seed_identical(200)
        groups = TaskAttempt.objects.usage_groups()
        assert len(groups) == 1
        assert groups[0].attempts == 200
        assert groups[0].input_tokens == 200 * 10

    def test_the_group_count_tracks_distinct_keys_not_rows(self) -> None:
        _seed_corpus()
        first = len(TaskAttempt.objects.usage_groups())
        _seed_corpus()
        assert len(TaskAttempt.objects.usage_groups()) == first
