"""The directive-loop guard chains with signal and budget enforcement.

Fail-closed and ordered: the first (most fundamental) refusal wins. Two chains split
by arc (#3643, #3649): the pre-admission INTAKE chain runs G1 flag and G4 budget; the
post-admission EXECUTION chain adds the signal-trust guard. Both reuse the outer
loop's probes.
"""

import datetime as dt

from django.test import TestCase

from teatree.core.factory.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.directive_loop import guards
from teatree.loops.shared.guards import GuardSeams


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


def _gap_report() -> FactorySignalsReport:
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
    return FactorySignalsReport(
        window_days=28, generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC), signals=[gap], verdict=SignalVerdict.RED
    )


def _open_seams() -> GuardSeams:
    return GuardSeams(signal_report=_healthy_report(), budget=BudgetVerdict.allow())


class TestExecutionGuards(TestCase):
    def test_healthy_signals_allow_execution_arc(self) -> None:
        verdict = guards.evaluate_execution_guards(
            seams=GuardSeams(signal_report=_healthy_report(), budget=BudgetVerdict.allow())
        )
        assert verdict.ok

    def test_budget_refusal_surfaces_the_reason(self) -> None:
        seams = GuardSeams(signal_report=_healthy_report(), budget=BudgetVerdict.skip("cap"))
        verdict = guards.evaluate_execution_guards(seams=seams)
        assert verdict.reason.startswith(guards.BUDGET)

    def test_untrusted_signal_refuses(self) -> None:
        seams = GuardSeams(signal_report=_gap_report(), budget=BudgetVerdict.allow())
        verdict = guards.evaluate_execution_guards(seams=seams)
        assert verdict.reason == guards.SIGNAL_UNTRUSTED

    def test_all_open_allows(self) -> None:
        verdict = guards.evaluate_execution_guards(seams=_open_seams())
        assert verdict.ok


class TestIntakeGuards(TestCase):
    """The pre-admission arc interprets and stops at the human ratify gate (#3643/#3649).

    It changes no config and merges nothing, so the score is not an admission
    precondition. The structural human ratify gate is. Other guards still apply.
    """

    def test_healthy_inputs_allow_intake(self) -> None:
        seams = GuardSeams(signal_report=_healthy_report(), budget=BudgetVerdict.allow())
        assert guards.evaluate_intake_guards(seams=seams).ok

    def test_untrusted_signal_still_allows(self) -> None:
        """An untrustworthy score gates VERIFYING, not interpreting owner intent.

        Intake already declares the factory score irrelevant by dropping G1b, and it
        reads no signal on any of its steps — so gating it on the trustworthiness of
        that same score blocked 25 captured directives behind a metric the arc never
        consults. The execution chain keeps G3 (``test_untrusted_signal_refuses``).
        """
        seams = GuardSeams(signal_report=_gap_report(), budget=BudgetVerdict.allow())
        verdict = guards.evaluate_intake_guards(seams=seams)
        assert verdict.ok
        assert verdict.reason == ""

    def test_budget_refusal_still_surfaces(self) -> None:
        seams = GuardSeams(signal_report=_healthy_report(), budget=BudgetVerdict.skip("cap"))
        verdict = guards.evaluate_intake_guards(seams=seams)
        assert verdict.reason.startswith(guards.BUDGET)
