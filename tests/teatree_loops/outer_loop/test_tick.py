"""The tick FSM dispatch — every advance branch fires once per tick (T4-PR-3).

Covers the branches beyond the propose→ratify→admit walk-through in
``test_propose.py``: implement, the merge-gated measure arming, the horizon-gated
decision, and the idle/waiting branches — all via injected seams so no live
critic, real merge, or real clock is needed.
"""

import datetime as dt
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec, Ticket
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.outer_loop import guards
from teatree.loops.outer_loop.guards import GuardSeams
from teatree.loops.outer_loop.tick import TickSeams, run_tick


def _live_critic() -> guards.CriticLiveness:
    return guards.CriticLiveness(live=True, verdict_count=guards.MIN_CRITIC_SAMPLE)


def _open_settings(*, measure_days: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        outer_loop_enabled=True,
        factory_score_enabled=True,
        outer_loop_measure_days=measure_days,
        outer_loop_max_per_week=99,
        outer_loop_stop_after_consecutive_failures=3,
    )


def _healthy_report() -> FactorySignalsReport:
    row = SignalRow(
        provider_id="review_catch",
        kind="quant",
        reading=SignalReading(value=0.9, sample_size=50, window_days=28, status=SignalStatus.OK),
        direction=Direction.HIGHER_IS_BETTER,
        red_when=None,
        baseline_value=0.9,
        delta=0.0,
        tripped=False,
        verdict=SignalVerdict.OK,
    )
    return FactorySignalsReport(
        window_days=28, generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC), signals=[row], verdict=SignalVerdict.OK
    )


def _seams(*, merged: bool | None = None, propose_report: FactorySignalsReport | None = None) -> TickSeams:
    return TickSeams(
        guards=GuardSeams(critic_probe=_live_critic, signal_report=_healthy_report(), budget=BudgetVerdict.allow()),
        propose_report=propose_report if propose_report is not None else _healthy_report(),
        merged_probe=(lambda _exp: merged) if merged is not None else None,
    )


class TestTickAdvanceBranches(TestCase):
    def _implementing(self) -> OuterLoopExperiment:
        exp = _make_experiment(
            hypothesis="H",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.OPERATOR,
            baseline_snapshot=_baseline(),
        )
        question = DeferredQuestion.record("Ratify?")
        DeferredQuestion.consume(question.pk, answer="approve")
        exp.attach_ratification(DeferredQuestion.objects.get(pk=question.pk))
        exp.admit()
        exp.begin_implementation(
            Ticket.objects.create(issue_url=f"https://e.com/{timezone.now().timestamp()}", role=Ticket.Role.AUTHOR)
        )
        return exp

    def test_admitted_advances_to_implementing(self) -> None:
        exp = _make_experiment(
            hypothesis="H",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.OPERATOR,
            baseline_snapshot=_baseline(),
        )
        question = DeferredQuestion.record("Ratify?")
        DeferredQuestion.consume(question.pk, answer="approve")
        exp.attach_ratification(DeferredQuestion.objects.get(pk=question.pk))
        exp.admit()
        result = run_tick(settings=_open_settings(), seams=_seams())
        assert result.action == "implementing"
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.IMPLEMENTING

    def test_implementing_waits_until_the_ticket_merges(self) -> None:
        self._implementing()
        result = run_tick(settings=_open_settings(), seams=_seams(merged=False))
        assert result.action == "waiting"
        assert result.reason == "implement_in_flight"

    def test_implementing_arms_measure_when_merged(self) -> None:
        exp = self._implementing()
        result = run_tick(settings=_open_settings(), seams=_seams(merged=True))
        assert result.action == "measuring"
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.MEASURING

    def test_measuring_waits_before_the_horizon(self) -> None:
        exp = self._implementing()
        exp.arm_measure(now=timezone.now())
        result = run_tick(settings=_open_settings(measure_days=7), seams=_seams())
        assert result.action == "waiting"
        assert result.reason == "horizon_not_elapsed"

    def test_measuring_decides_after_the_horizon(self) -> None:
        exp = self._implementing()
        exp.arm_measure(now=timezone.now() - dt.timedelta(days=30))
        result = run_tick(settings=_open_settings(measure_days=7), seams=_seams())
        assert result.action in {"kept", "revert_pending"}

    def test_revert_pending_asks_then_awaits_a_human(self) -> None:
        exp = self._implementing()
        exp.arm_measure()
        exp.request_revert(post_snapshot=_baseline(), reason="no improvement")
        # First tick asks the human to revert (records the DeferredQuestion).
        first = run_tick(settings=_open_settings(), seams=_seams())
        assert first.action == "revert_asked"
        exp.refresh_from_db()
        assert exp.revert_question is not None
        # Subsequent ticks wait for `t3 outer resolve-revert` (no dead-end, no auto-revert).
        second = run_tick(settings=_open_settings(), seams=_seams())
        assert second.action == "waiting"
        assert second.reason == "awaiting_human_revert"
        assert second.experiment_id == exp.pk

    def test_no_target_signal_is_idle(self) -> None:
        # Admission is open and no experiment is active, but the report is healthy
        # → no proposal candidate → idle (not a spurious experiment).
        result = run_tick(settings=_open_settings(), seams=_seams(propose_report=_healthy_report()))
        assert result.action == "idle"
        assert result.reason == "no_target_signal"
        assert OuterLoopExperiment.objects.count() == 0

    def test_weekly_cap_idles_without_a_park(self) -> None:
        # A terminal experiment this week, none active → weekly cap idles (not a
        # convergence park): a mere wait, no human question.
        done = _make_experiment(
            hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
        )
        OuterLoopExperiment.objects.filter(pk=done.pk).update(state=OuterLoopExperiment.State.KEPT)
        settings = _open_settings()
        settings.outer_loop_max_per_week = 1
        result = run_tick(settings=settings, seams=_seams())
        assert result.action == "idle"
        assert result.reason == guards.WEEKLY_CAP

    def test_convergence_park_is_deduped_across_ticks(self) -> None:
        for _ in range(3):
            done = _make_experiment(
                hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
            )
            OuterLoopExperiment.objects.filter(pk=done.pk).update(state=OuterLoopExperiment.State.REVERTED)
        run_tick(settings=_open_settings(), seams=_seams())
        run_tick(settings=_open_settings(), seams=_seams())
        assert DeferredQuestion.objects.filter(options_hash="outer_loop_converged:").count() == 1

    def test_real_merged_probe_reads_the_ticket_state(self) -> None:
        exp = self._implementing()
        OuterLoopExperiment.objects.filter(pk=exp.pk).update(state=OuterLoopExperiment.State.IMPLEMENTING)
        Ticket.objects.filter(pk=exp.ticket_id).update(state=Ticket.State.MERGED)
        # No merged_probe injected → the real _ticket_merged reads the ticket state.
        result = run_tick(settings=_open_settings(), seams=_seams())
        assert result.action == "measuring"


def _baseline() -> FactoryScoreSnapshot:
    return FactoryScoreSnapshot.objects.create(
        overlay="", window_days=7, recipe_sha="s", aggregate=0.7, verdict="ok", coverage=1.0, coverage_floor=0.6
    )


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
