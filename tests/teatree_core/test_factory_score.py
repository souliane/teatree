"""Anti-vacuity tests for the recipe-weighted factory score (SIG-PR-2).

Two lanes. The pure lane folds constructed signal reports so the aggregate is
checked against exact hand-computed numbers and a degraded twin is proven strictly
lower — the score reflects the signals, never a constant. The DB lane runs the real
``compute_factory_signals`` path over a production-shaped ledger (a healthy factory
scores a covered number; an empty ledger can never be ``ok``) and pins the snapshot
deltas.
"""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory_recipe import load_recipe
from teatree.core.factory_score import FactoryScore, score, score_report
from teatree.core.factory_signals import (
    Direction,
    FactorySignalsReport,
    SignalReading,
    SignalRow,
    SignalStatus,
    SignalVerdict,
)
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from tests.factories import (
    MergeAuditFactory,
    MergeClearFactory,
    RedMrFixAttemptFactory,
    ReviewVerdictFactory,
    SessionFactory,
    TaskAttemptFactory,
    TaskFactory,
    TicketFactory,
    TicketTransitionFactory,
)

_DIRECTIONS = {
    "first_try_green": Direction.HIGHER_IS_BETTER,
    "defect_escape": Direction.LOWER_IS_BETTER,
    "review_catch": Direction.HIGHER_IS_BETTER,
    "merge_latency": Direction.LOWER_IS_BETTER,
    "repair_burn": Direction.LOWER_IS_BETTER,
}

# A healthy reading per signal and the 0..1 normalisation the committed recipe
# (caps 48.0 / 5.0) maps it to — hand-computed so the aggregate is exact.
_HEALTHY = {
    "first_try_green": (0.9, 0.9),
    "defect_escape": (0.1, 0.9),
    "review_catch": (0.8, 0.8),
    "merge_latency": (12.0, 0.75),
    "repair_burn": (1.5, 0.7),
}
_WEIGHTS = {
    "first_try_green": 0.25,
    "defect_escape": 0.25,
    "review_catch": 0.20,
    "merge_latency": 0.15,
    "repair_burn": 0.15,
}


def _row(
    provider_id: str,
    value: float,
    *,
    status: SignalStatus = SignalStatus.OK,
    verdict: SignalVerdict = SignalVerdict.OK,
) -> SignalRow:
    return SignalRow(
        provider_id=provider_id,
        kind="quant",
        reading=SignalReading(value=value, sample_size=10, window_days=28, status=status),
        direction=_DIRECTIONS[provider_id],
        red_when=None,
        baseline_value=None,
        delta=None,
        tripped=False,
        verdict=verdict,
    )


def _report(rows: list[SignalRow]) -> FactorySignalsReport:
    return FactorySignalsReport(
        window_days=28,
        generated_at=timezone.now(),
        signals=rows,
        verdict=SignalVerdict.OK,
    )


def _healthy_rows() -> list[SignalRow]:
    return [_row(pid, value) for pid, (value, _norm) in _HEALTHY.items()]


def _expected_aggregate(overrides: dict[str, float] | None = None) -> float:
    norms = {pid: norm for pid, (_v, norm) in _HEALTHY.items()}
    if overrides:
        norms.update(overrides)
    return sum(_WEIGHTS[pid] * norms[pid] for pid in norms)


class TestPureFold:
    def setup_method(self) -> None:
        self.recipe = load_recipe()

    def test_known_fixture_yields_expected_aggregate(self) -> None:
        result = score_report(self.recipe, _report(_healthy_rows()))
        assert result.verdict == SignalVerdict.OK.value
        assert result.aggregate == pytest.approx(0.8275)
        assert result.coverage == pytest.approx(1.0)

    def test_degraded_signal_yields_strictly_lower_score(self) -> None:
        healthy = score_report(self.recipe, _report(_healthy_rows()))
        rows = _healthy_rows()
        # Drop first_try_green from 0.9 to 0.6 (still above its 0.5 red floor).
        rows[0] = _row("first_try_green", 0.6)
        degraded = score_report(self.recipe, _report(rows))
        assert degraded.verdict == SignalVerdict.OK.value
        assert degraded.aggregate == pytest.approx(_expected_aggregate({"first_try_green": 0.6}))
        assert degraded.aggregate < healthy.aggregate

    def test_instrumentation_gap_folds_whole_score_red(self) -> None:
        rows = _healthy_rows()
        rows[0] = _row(
            "first_try_green",
            0.0,
            status=SignalStatus.INSTRUMENTATION_GAP,
            verdict=SignalVerdict.INSTRUMENTATION_GAP,
        )
        result = score_report(self.recipe, _report(rows))
        assert result.verdict == SignalVerdict.RED.value
        assert result.aggregate is None

    def test_red_report_row_folds_whole_score_red(self) -> None:
        rows = _healthy_rows()
        rows[3] = _row("merge_latency", 12.0, verdict=SignalVerdict.RED)
        result = score_report(self.recipe, _report(rows))
        assert result.verdict == SignalVerdict.RED.value
        assert result.aggregate is None

    def test_recipe_red_floor_trip_folds_red(self) -> None:
        rows = _healthy_rows()
        # first_try_green red_when is 0.5 (HIGHER_IS_BETTER): 0.4 trips it.
        rows[0] = _row("first_try_green", 0.4)
        result = score_report(self.recipe, _report(rows))
        assert result.verdict == SignalVerdict.RED.value
        assert result.aggregate is None

    def test_below_coverage_floor_yields_none_and_red(self) -> None:
        # coverage_floor is 0.6; only 2 of 5 covered → 0.4 < 0.6 → None + RED.
        rows = _healthy_rows()
        for i in (0, 1, 2):
            rows[i] = _row(rows[i].provider_id, 0.0, status=SignalStatus.INSUFFICIENT_DATA)
        result = score_report(self.recipe, _report(rows))
        assert result.coverage == pytest.approx(0.4)
        assert result.aggregate is None
        assert result.verdict == SignalVerdict.RED.value

    def test_uncapped_reading_outside_unit_is_red_not_clamped(self) -> None:
        rows = _healthy_rows()
        # defect_escape is uncapped; a reading of 1.5 is out of [0,1] → RED (never clamped).
        rows[1] = _row("defect_escape", 1.5)
        result = score_report(self.recipe, _report(rows))
        assert result.verdict == SignalVerdict.RED.value
        assert result.aggregate is None

    def test_regressing_row_keeps_number_but_regressing_verdict(self) -> None:
        rows = _healthy_rows()
        rows[2] = _row("review_catch", 0.8, verdict=SignalVerdict.REGRESSING)
        result = score_report(self.recipe, _report(rows))
        assert result.verdict == SignalVerdict.REGRESSING.value
        assert result.aggregate == pytest.approx(0.8275)

    def test_recipe_approved_stamps_on_matching_sha(self) -> None:
        approved = score_report(self.recipe, _report(_healthy_rows()), approved_recipe_sha=self.recipe.recipe_sha)
        unapproved = score_report(self.recipe, _report(_healthy_rows()), approved_recipe_sha="")
        stale = score_report(self.recipe, _report(_healthy_rows()), approved_recipe_sha="different")
        assert approved.recipe_approved is True
        assert unapproved.recipe_approved is False
        assert stale.recipe_approved is False


class TestDbScorePath(TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()

    def _merge(self, *, pr_id: int, days_ago: float, latency_hours: float = 1.0, reviewed: str = "") -> None:
        merged_at = self.now - timedelta(days=days_ago)
        issued_at = merged_at - timedelta(hours=latency_hours)
        clear = MergeClearFactory(pr_id=pr_id, slug="souliane/teatree", issued_at=issued_at, consumed_at=merged_at)
        MergeAuditFactory(clear=clear, merged_at=merged_at)
        if reviewed == "hold":
            ReviewVerdictFactory(hold=True, slug="souliane/teatree", pr_id=pr_id)

    def _attempt(self, *, days_ago: float, iteration: int) -> None:
        ticket = TicketFactory()
        session = SessionFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket, session=session, phase="coding")
        att = TaskAttemptFactory(task=task, iteration=iteration, exit_code=0)
        TaskAttempt.objects.filter(pk=att.pk).update(started_at=self.now - timedelta(days=days_ago))

    def _seed_healthy_factory(self) -> None:
        # Current-window merges feed S1/S3/S4; one red keeps S1 off instrumentation_gap.
        for i in range(6):
            self._merge(pr_id=901 + i, days_ago=5, latency_hours=2.0, reviewed="hold" if i < 3 else "")
        RedMrFixAttemptFactory(
            pr_url="https://github.com/souliane/teatree/pull/901",
            head_sha=f"{901:040x}",
            dispatched_at=self.now - timedelta(days=5),
        )
        # Preceding-window merges are the S2 defect-escape denominator.
        for i in range(6):
            self._merge(pr_id=800 + i, days_ago=40)
        fix = TicketFactory(kind=Ticket.Kind.FIX)
        tr = TicketTransitionFactory(ticket=fix)
        TicketTransition.objects.filter(pk=tr.pk).update(created_at=self.now - timedelta(days=5))
        # Successful attempts give S5 a covered burn reading.
        for _ in range(6):
            self._attempt(days_ago=5, iteration=1)

    def test_healthy_factory_scores_a_covered_number(self) -> None:
        self._seed_healthy_factory()
        result = score(now=self.now)
        assert isinstance(result, FactoryScore)
        assert result.aggregate is not None
        assert result.verdict in {SignalVerdict.OK.value, SignalVerdict.REGRESSING.value}
        assert result.coverage >= load_recipe().coverage_floor

    def test_empty_ledger_negative_control_is_never_ok(self) -> None:
        result = score(now=self.now)
        # Every signal is insufficient_data → coverage 0 < floor → None + RED.
        assert result.aggregate is None
        assert result.verdict == SignalVerdict.RED.value

    def test_delta_vs_previous_diffs_the_last_snapshot(self) -> None:
        self._seed_healthy_factory()
        first = score(now=self.now)
        FactoryScoreSnapshot.objects.record_snapshot(first, tree_sha="a")
        second = score(now=self.now)
        assert second.delta_vs_previous == pytest.approx(second.aggregate - first.aggregate)

    def test_delta_vs_previous_is_none_without_snapshot(self) -> None:
        self._seed_healthy_factory()
        assert score(now=self.now).delta_vs_previous is None
