"""The QUADRUPLE-OFF guard chain — every guard demonstrably load-bearing (T4-PR-3).

The chain is fail-closed: each guard is proven to REFUSE with its own typed
reason, and the positive (allow) path is exercisable only via injected fakes
this session (the real critic + honest signals do not exist yet). The
admission-verdict caps (concurrency / weekly / convergence) are proven against
the real DB.
"""

import datetime as dt
from types import SimpleNamespace

from django.test import TestCase

from teatree.core import models as core_models
from teatree.core.factory_signal_queries import SignalReading, SignalStatus
from teatree.core.factory_signals import Direction, FactorySignalsReport, SignalRow, SignalVerdict
from teatree.core.models import FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.outer_loop import guards


def _settings(
    *, enabled: bool = True, score: bool = True, max_per_week: int = 1, stop_after: int = 3
) -> SimpleNamespace:
    return SimpleNamespace(
        outer_loop_enabled=enabled,
        factory_score_enabled=score,
        outer_loop_max_per_week=max_per_week,
        outer_loop_stop_after_consecutive_failures=stop_after,
    )


def _row(provider_id: str, status: SignalStatus) -> SignalRow:
    return SignalRow(
        provider_id=provider_id,
        kind="quant",
        reading=SignalReading(value=0.9, sample_size=50, window_days=28, status=status),
        direction=Direction.HIGHER_IS_BETTER,
        red_when=None,
        baseline_value=0.9,
        delta=0.0,
        tripped=False,
        verdict=SignalVerdict.OK if status == SignalStatus.OK else SignalVerdict.INSTRUMENTATION_GAP,
    )


def _report(*statuses: SignalStatus) -> FactorySignalsReport:
    return FactorySignalsReport(
        window_days=28,
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        signals=[_row(f"s{i}", status) for i, status in enumerate(statuses)],
        verdict=SignalVerdict.OK,
    )


def _live_critic() -> guards.CriticLiveness:
    return guards.CriticLiveness(live=True, verdict_count=guards.MIN_CRITIC_SAMPLE)


_ALL_OK = (SignalStatus.OK, SignalStatus.OK)
_DB_DOWN = "db down"


class TestGuardChainRefusals:
    def test_flag_off_refuses_first(self) -> None:
        verdict = guards.evaluate_guards(settings=_settings(enabled=False))
        assert verdict.ok is False
        assert verdict.reason == guards.FLAG_OFF

    def test_flag_on_but_score_off_refuses(self) -> None:
        # G1b: the loop needs the T4-PR-2 metric — with factory_score_enabled off
        # it refuses BEFORE the critic probe (and before any snapshot could be written).
        verdict = guards.evaluate_guards(settings=_settings(score=False))
        assert verdict.reason == guards.SCORE_OFF

    def test_flag_on_but_critic_not_live_refuses(self) -> None:
        # The default critic probe fails CLOSED — the critic model does not exist
        # this session, so even with the flags on the tick refuses critic_not_live.
        verdict = guards.evaluate_guards(settings=_settings(), seams=guards.GuardSeams(signal_report=_report(*_ALL_OK)))
        assert verdict.reason == guards.CRITIC_NOT_LIVE

    def test_signal_gap_refuses_even_with_a_live_critic(self) -> None:
        verdict = guards.evaluate_guards(
            settings=_settings(),
            seams=guards.GuardSeams(
                critic_probe=_live_critic,
                signal_report=_report(SignalStatus.OK, SignalStatus.INSTRUMENTATION_GAP),
            ),
        )
        assert verdict.reason == guards.SIGNAL_UNTRUSTED

    def test_budget_refusal_surfaces_its_reason(self) -> None:
        verdict = guards.evaluate_guards(
            settings=_settings(),
            seams=guards.GuardSeams(
                critic_probe=_live_critic,
                signal_report=_report(*_ALL_OK),
                budget=BudgetVerdict.skip("low_ram (used=99%)"),
            ),
        )
        assert verdict.reason.startswith(guards.BUDGET)
        assert "low_ram" in verdict.reason

    def test_all_gates_open_allows(self) -> None:
        # Positive path — exercisable only via injected fakes this session.
        verdict = guards.evaluate_guards(
            settings=_settings(),
            seams=guards.GuardSeams(
                critic_probe=_live_critic,
                signal_report=_report(*_ALL_OK),
                budget=BudgetVerdict.allow(),
            ),
        )
        assert verdict.ok is True


class TestCriticProbeFailsClosed:
    def test_default_probe_reports_not_live_when_model_absent(self) -> None:
        # No CriticVerdict model exists this session → the defensive probe fails
        # closed (never an ImportError, never a spurious live).
        liveness = guards.probe_critic_liveness()
        assert liveness.live is False

    def test_probe_goes_live_once_the_critic_has_enough_verdicts(self, monkeypatch) -> None:
        # The forward-compat path: when the sibling critic PR lands a CriticVerdict
        # model, the same probe reports live once it has >= MIN_CRITIC_SAMPLE rows.
        class _FakeManager:
            def __init__(self, count: int) -> None:
                self._count = count

            def count(self) -> int:
                return self._count

        class _FakeCriticVerdict:
            objects = _FakeManager(guards.MIN_CRITIC_SAMPLE)

        monkeypatch.setattr(core_models, "CriticVerdict", _FakeCriticVerdict, raising=False)
        assert guards.probe_critic_liveness().live is True

        _FakeCriticVerdict.objects = _FakeManager(guards.MIN_CRITIC_SAMPLE - 1)
        assert guards.probe_critic_liveness().live is False

    def test_probe_fails_closed_when_the_count_query_raises(self, monkeypatch) -> None:
        # A model present but its count() raising (a broken DB) must fail CLOSED,
        # never crash the tick.
        class _RaisingManager:
            def count(self) -> int:
                raise RuntimeError(_DB_DOWN)

        class _FakeCriticVerdict:
            objects = _RaisingManager()

        monkeypatch.setattr(core_models, "CriticVerdict", _FakeCriticVerdict, raising=False)
        assert guards.probe_critic_liveness().live is False


class TestSignalTrust:
    def test_trusted_when_no_gap(self) -> None:
        trust = guards.probe_signal_trust(report=_report(SignalStatus.OK, SignalStatus.OK))
        assert trust.trusted is True
        assert trust.gap_ids == ()

    def test_untrusted_names_the_gap(self) -> None:
        trust = guards.probe_signal_trust(report=_report(SignalStatus.INSTRUMENTATION_GAP, SignalStatus.OK))
        assert trust.trusted is False
        assert trust.gap_ids == ("s0",)


class TestSignalTrustRealCompute(TestCase):
    def test_computes_the_live_report_when_none_given(self) -> None:
        # No report injected → the probe computes the real factory signals over the
        # (empty) ledger. An empty ledger reports insufficient_data, not a gap, so it
        # is trusted — the probe just surfaces whatever the recorders report.
        trust = guards.probe_signal_trust()
        assert isinstance(trust.trusted, bool)


class TestAdmissionVerdict(TestCase):
    def _propose(self, *, overlay: str = "") -> None:
        _make_experiment(
            hypothesis="H",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.OPERATOR,
            overlay=overlay,
        )

    def test_concurrency_cap_blocks_a_second_proposal(self) -> None:
        self._propose()
        verdict = guards.admission_verdict(settings=_settings())
        assert verdict.reason == guards.CONCURRENCY_CAP

    def test_weekly_cap_blocks_after_the_limit(self) -> None:
        # Terminalise the first so it does not trip concurrency, but it still
        # counts toward the weekly cap.
        self._propose()
        OuterLoopExperiment.objects.all().update(state=OuterLoopExperiment.State.REJECTED)
        verdict = guards.admission_verdict(settings=_settings(max_per_week=1))
        assert verdict.reason == guards.WEEKLY_CAP

    def test_convergence_brake_parks_after_three_non_kept(self) -> None:
        for _ in range(3):
            exp = _make_experiment(
                hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
            )
            OuterLoopExperiment.objects.filter(pk=exp.pk).update(state=OuterLoopExperiment.State.REVERTED)
        verdict = guards.admission_verdict(settings=_settings(max_per_week=99, stop_after=3))
        assert verdict.reason == guards.CONVERGED

    def test_clean_slate_admits(self) -> None:
        verdict = guards.admission_verdict(settings=_settings(max_per_week=1))
        assert verdict.ok is True


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
