"""Outer-loop supervision, signal, budget, and proposal bounds."""

import datetime as dt

from django.test import TestCase

from teatree.core.factory.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.core.models import OuterLoopExperiment, ProposalSpec
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.outer_loop import guards
from teatree.loops.outer_loop.tick import TickSeams, run_tick
from teatree.loops.shared import guards as shared_guards


def _report(status: SignalStatus = SignalStatus.OK) -> FactorySignalsReport:
    return FactorySignalsReport(
        window_days=28,
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        signals=[
            SignalRow(
                provider_id="review_catch",
                kind="quant",
                reading=SignalReading(value=0.9, sample_size=50, window_days=28, status=status),
                direction=Direction.HIGHER_IS_BETTER,
                red_when=None,
                baseline_value=0.9,
                delta=0.0,
                tripped=False,
                verdict=SignalVerdict.OK if status == SignalStatus.OK else SignalVerdict.INSTRUMENTATION_GAP,
            )
        ],
        verdict=SignalVerdict.OK,
    )


class TestLiveSupervision(TestCase):
    def test_unconditional_merge_enforcement_allows_outer_tick_to_reach_proposal_selection(self) -> None:
        result = run_tick(
            seams=TickSeams(
                guards=shared_guards.GuardSeams(signal_report=_report(), budget=BudgetVerdict.allow()),
                propose_report=_report(),
            )
        )
        assert result.action == "idle"
        assert result.reason == "no_target_signal"


class TestOtherGuards(TestCase):
    def test_signal_gap_refuses(self) -> None:
        verdict = guards.evaluate_guards(
            seams=shared_guards.GuardSeams(
                signal_report=_report(SignalStatus.INSTRUMENTATION_GAP),
                budget=BudgetVerdict.allow(),
            )
        )
        assert verdict.reason == shared_guards.SIGNAL_UNTRUSTED

    def test_budget_refusal_surfaces_reason(self) -> None:
        verdict = guards.evaluate_guards(
            seams=shared_guards.GuardSeams(
                signal_report=_report(),
                budget=BudgetVerdict.skip("low_ram"),
            )
        )
        assert verdict.reason == "budget:low_ram"

    def test_code_bounds_apply_to_new_proposals(self) -> None:
        assert (guards.MAX_PER_WEEK, guards.MEASURE_DAYS, guards.STOP_AFTER_CONSECUTIVE_FAILURES) == (1, 7, 3)
        first = OuterLoopExperiment.objects.propose(
            ProposalSpec(
                hypothesis="H",
                target_provider_id="review_catch",
                source=OuterLoopExperiment.Source.OPERATOR,
            )
        )
        assert guards.admission_verdict().reason == guards.CONCURRENCY_CAP
        OuterLoopExperiment.objects.filter(pk=first.pk).update(state=OuterLoopExperiment.State.REJECTED)
        assert guards.admission_verdict().reason == guards.WEEKLY_CAP
