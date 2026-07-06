"""The directive-loop guard chain — the code half of QUADRUPLE-OFF (north-star PR-7).

Fail-closed and ordered: the first (most fundamental) refusal wins. G1 flag, G1b
score, G2 critic-live, G3 signal-trust, G4 budget — reusing the outer loop's probes.
"""

import datetime as dt
from types import SimpleNamespace

from django.test import TestCase

from teatree.core.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.directive_loop import guards
from teatree.loops.outer_loop.guards import CriticLiveness, GuardSeams, probe_critic_liveness


def _live_critic() -> CriticLiveness:
    return CriticLiveness(live=True, verdict_count=probe_critic_liveness().verdict_count or 5)


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


def _settings(*, flag: bool = True, score: bool = True) -> SimpleNamespace:
    return SimpleNamespace(directive_loop_enabled=flag, factory_score_enabled=score, directive_verify_days=7)


def _open_seams() -> GuardSeams:
    return GuardSeams(critic_probe=_live_critic, signal_report=_healthy_report(), budget=BudgetVerdict.allow())


class TestEvaluateGuards(TestCase):
    def test_flag_off_refuses_first(self) -> None:
        verdict = guards.evaluate_guards(settings=_settings(flag=False), seams=_open_seams())
        assert not verdict.ok
        assert verdict.reason == guards.FLAG_OFF

    def test_score_off_refuses_before_critic(self) -> None:
        verdict = guards.evaluate_guards(settings=_settings(score=False), seams=_open_seams())
        assert verdict.reason == guards.SCORE_OFF

    def test_critic_not_live_refuses(self) -> None:
        seams = GuardSeams(
            critic_probe=lambda: CriticLiveness(live=False, verdict_count=0),
            signal_report=_healthy_report(),
            budget=BudgetVerdict.allow(),
        )
        verdict = guards.evaluate_guards(settings=_settings(), seams=seams)
        assert verdict.reason == guards.CRITIC_NOT_LIVE

    def test_budget_refusal_surfaces_the_reason(self) -> None:
        seams = GuardSeams(critic_probe=_live_critic, signal_report=_healthy_report(), budget=BudgetVerdict.skip("cap"))
        verdict = guards.evaluate_guards(settings=_settings(), seams=seams)
        assert verdict.reason.startswith(guards.BUDGET)

    def test_untrusted_signal_refuses(self) -> None:
        gap = SignalRow(
            provider_id="review_catch",
            kind="quant",
            reading=SignalReading(value=0.0, sample_size=0, window_days=28, status=SignalStatus.INSTRUMENTATION_GAP),
            direction=Direction.HIGHER_IS_BETTER,
            red_when=None,
            baseline_value=0.0,
            delta=0.0,
            tripped=False,
            verdict=SignalVerdict.RED,
        )
        report = FactorySignalsReport(
            window_days=28,
            generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            signals=[gap],
            verdict=SignalVerdict.RED,
        )
        seams = GuardSeams(critic_probe=_live_critic, signal_report=report, budget=BudgetVerdict.allow())
        verdict = guards.evaluate_guards(settings=_settings(), seams=seams)
        assert verdict.reason == guards.SIGNAL_UNTRUSTED

    def test_all_open_allows(self) -> None:
        verdict = guards.evaluate_guards(settings=_settings(), seams=_open_seams())
        assert verdict.ok
