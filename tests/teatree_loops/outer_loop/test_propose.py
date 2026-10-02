"""PROPOSE selection + the gates-opened pipeline (anti-vacuity, T4-PR-3).

Proves the loop is WIRED, not dead: with every gate opened in a harness (live
critic, honest signals, allowed budget) a tick proposes an experiment from a
regressing signal, the next tick asks for ratification, and the experiment reaches
ADMITTED only AFTER a human answer approves — never auto-implemented.
"""

import datetime as dt
from types import SimpleNamespace

import pytest
from django.test import TestCase

from teatree.core.factory.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.outer_loop import guards
from teatree.loops.outer_loop.guards import GuardSeams
from teatree.loops.outer_loop.propose import operator_proposal, select_proposal
from teatree.loops.outer_loop.tick import TickSeams, run_tick


def _row(provider_id: str, verdict: SignalVerdict, *, status: SignalStatus = SignalStatus.OK) -> SignalRow:
    return SignalRow(
        provider_id=provider_id,
        kind="quant",
        reading=SignalReading(value=0.4, sample_size=50, window_days=28, status=status),
        direction=Direction.HIGHER_IS_BETTER,
        red_when=None,
        baseline_value=0.6,
        delta=-0.2,
        tripped=False,
        verdict=verdict,
    )


def _report(*rows: SignalRow) -> FactorySignalsReport:
    return FactorySignalsReport(
        window_days=28,
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        signals=list(rows),
        verdict=SignalVerdict.REGRESSING,
    )


def _open_settings() -> SimpleNamespace:
    return SimpleNamespace(
        outer_loop_enabled=True,
        factory_score_enabled=True,
        outer_loop_measure_days=7,
        outer_loop_max_per_week=1,
        outer_loop_stop_after_consecutive_failures=3,
    )


def _live_critic() -> guards.CriticLiveness:
    return guards.CriticLiveness(live=True, verdict_count=guards.MIN_CRITIC_SAMPLE)


class TestSelectProposal:
    def test_picks_the_regressing_signal(self) -> None:
        report = _report(
            _row("first_try_green", SignalVerdict.OK),
            _row("review_catch", SignalVerdict.REGRESSING),
        )
        candidate = select_proposal(report=report)
        assert candidate is not None
        assert candidate.target_provider_id == "review_catch"
        assert candidate.source == OuterLoopExperiment.Source.SIGNAL_REGRESSION

    def test_red_outranks_regressing(self) -> None:
        report = _report(
            _row("review_catch", SignalVerdict.REGRESSING),
            _row("first_try_green", SignalVerdict.RED),
        )
        candidate = select_proposal(report=report)
        assert candidate is not None
        assert candidate.target_provider_id == "first_try_green"

    def test_no_candidate_when_all_healthy(self) -> None:
        report = _report(_row("first_try_green", SignalVerdict.OK))
        assert select_proposal(report=report) is None

    def test_instrumentation_gap_is_not_a_target(self) -> None:
        # A gap is G3's refusal, never something to optimise against.
        report = _report(
            _row("review_catch", SignalVerdict.INSTRUMENTATION_GAP, status=SignalStatus.INSTRUMENTATION_GAP)
        )
        assert select_proposal(report=report) is None

    def test_operator_proposal_is_operator_sourced(self) -> None:
        candidate = operator_proposal("Do the thing", "merge_latency", regress_band=0.1)
        assert candidate.source == OuterLoopExperiment.Source.OPERATOR
        assert candidate.regress_band == pytest.approx(0.1)


class TestGatesOpenedPipeline(TestCase):
    def _tick(self, report: FactorySignalsReport) -> object:
        return run_tick(
            settings=_open_settings(),
            seams=TickSeams(
                guards=GuardSeams(critic_probe=_live_critic, signal_report=report, budget=BudgetVerdict.allow()),
                propose_report=report,
            ),
        )

    def test_open_gates_propose_then_ratify_then_admit_only_after_answer(self) -> None:
        report = _report(_row("review_catch", SignalVerdict.REGRESSING), _row("first_try_green", SignalVerdict.OK))

        # Tick 1 — a proposal is created (the loop is wired, not dead).
        first = self._tick(report)
        assert first.action == "proposed"
        exp = OuterLoopExperiment.objects.get(pk=first.experiment_id)
        assert exp.state == OuterLoopExperiment.State.PROPOSED
        assert exp.baseline_snapshot is not None

        # Tick 2 — ratification is ASKED (a DeferredQuestion), NOT auto-admitted.
        second = self._tick(report)
        assert second.action == "ratify_asked"
        exp.refresh_from_db()
        assert exp.state == OuterLoopExperiment.State.RATIFY_PENDING
        assert exp.ratify_question is not None

        # Tick 3 — still pending until a human answers: never auto-implements.
        third = self._tick(report)
        assert third.action == "pending"
        exp.refresh_from_db()
        assert exp.state == OuterLoopExperiment.State.RATIFY_PENDING

        # A human approves → the NEXT tick admits it (the only ADMITTED path).
        DeferredQuestion.consume(exp.ratify_question_id, answer="approve")
        fourth = self._tick(report)
        assert fourth.action == "admitted"
        exp.refresh_from_db()
        assert exp.state == OuterLoopExperiment.State.ADMITTED

    def test_convergence_brake_parks_instead_of_proposing(self) -> None:
        for _ in range(3):
            done = _make_experiment(
                hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
            )
            OuterLoopExperiment.objects.filter(pk=done.pk).update(state=OuterLoopExperiment.State.REVERTED)
        report = _report(_row("review_catch", SignalVerdict.REGRESSING))
        result = self._tick(report)
        assert result.action == "parked"
        assert result.reason == guards.CONVERGED
        # No fourth experiment was proposed; a park question was recorded once.
        assert OuterLoopExperiment.objects.count() == 3
        assert DeferredQuestion.objects.filter(options_hash="outer_loop_converged:").count() == 1


def _make_experiment(
    *,
    overlay: str = "",
    baseline_snapshot: FactoryScoreSnapshot | None = None,
    **spec_kw: object,
) -> OuterLoopExperiment:
    """Build an experiment via the ProposalSpec factory (test convenience)."""
    return OuterLoopExperiment.objects.propose(
        ProposalSpec(**spec_kw), overlay=overlay, baseline_snapshot=baseline_snapshot
    )
