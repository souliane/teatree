"""Scheduler meta-tests: budget skipping, tier filtering, lease, Slack cap downgrade."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.models import SelfImproveFiring
from teatree.loop.self_improve import ActionRung, BudgetVerdict, DetectorReport, Tier, record_firing, run_tier
from teatree.loop.self_improve.schedule import detectors_for_tier


@dataclass(slots=True)
class _StubDetector:
    """Minimal stub detector for scheduler tests."""

    name: ClassVar[str] = "stub"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "warn"
    max_rung: ClassVar[str] = ActionRung.SLACK
    auto_fix: ClassVar[bool] = False

    detector_name: str = "stub_detector"
    state_value: str = "h1"

    def detect(self) -> list[DetectorReport]:
        return [
            DetectorReport(
                detector=self.detector_name,
                dedup_key=f"{self.detector_name}::x",
                state_hash=self.state_value,
                severity="warn",
                max_rung=ActionRung.SLACK,
                summary="stub",
                payload={"slack_channel": "C0"},
            )
        ]

    def scan(self) -> list[object]:
        return []


class SchedulerMetaTests(TestCase):
    def test_budget_skip_short_circuits_scan(self) -> None:
        detector = _StubDetector()
        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.skip("low_ram (used=92%)"),
        )
        assert result.skipped is True
        assert result.reports == []
        assert result.actions == []
        assert SelfImproveFiring.objects.count() == 0

    def test_tier_filtering_cheap_only_in_phase_1(self) -> None:
        cheap = detectors_for_tier(Tier.CHEAP)
        medium = detectors_for_tier(Tier.MEDIUM)
        expensive = detectors_for_tier(Tier.EXPENSIVE)
        all_ = detectors_for_tier(Tier.ALL)
        unknown = detectors_for_tier("phase-99-future")
        # Phase 1 only ships cheap detectors.
        assert len(cheap) == 3
        assert medium == []
        assert expensive == []
        assert len(all_) == 3
        assert unknown == []

    def test_tier_runs_all_detectors_then_advances_ladder(self) -> None:
        detector = _StubDetector()
        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
        )
        assert result.skipped is False
        assert len(result.reports) == 1
        assert len(result.actions) == 1
        assert result.actions[0].rung == ActionRung.STATUSLINE

    def test_slack_rate_cap_downgrade_through_scheduler(self) -> None:
        # Seed a prior slack firing in the cap window.
        seed = DetectorReport(
            detector="other",
            dedup_key="other::y",
            state_hash="seed",
            severity="error",
            max_rung=ActionRung.SLACK,
            summary="seed",
        )
        record_firing(seed, action=ActionRung.SLACK)
        # Force-escalate the stub detector to slack rung by pre-recording
        # a statusline-rung firing, then run the tier with a different
        # state_hash.
        ladder_first = DetectorReport(
            detector="stub_detector",
            dedup_key="stub_detector::x",
            state_hash="h0",
            severity="warn",
            max_rung=ActionRung.SLACK,
            summary="seed",
        )
        record_firing(ladder_first, action=ActionRung.STATUSLINE)
        detector = _StubDetector(state_value="h1")  # different state_hash ⇒ escalate
        messaging = MagicMock()
        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            messaging=messaging,
            budget=BudgetVerdict.allow(),
        )
        # Slack cap hit ⇒ downgrade.
        assert len(result.actions) == 1
        assert result.actions[0].rung == ActionRung.STATUSLINE
        assert result.actions[0].slack_capped is True
        messaging.post_message.assert_not_called()

    def test_lease_contention_simulated_by_skipping_run(self) -> None:
        """A skipped budget verdict mirrors the lease-contention skip path.

        Both shapes return ``skipped=True`` with no DB writes — the
        mgmt command's lease-acquire check returns early via the same
        contract.
        """
        detector = _StubDetector()
        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.skip("another self-improve cycle is already running"),
        )
        assert result.skipped is True
        assert SelfImproveFiring.objects.count() == 0


class _CountingDetector:
    """Used to verify the scheduler iterates each detector exactly once."""

    name: ClassVar[str] = "counting"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "info"
    max_rung: ClassVar[str] = ActionRung.STATUSLINE
    auto_fix: ClassVar[bool] = False

    def __init__(self) -> None:
        self.scan_count = 0

    def detect(self) -> list[DetectorReport]:
        self.scan_count += 1
        return []

    def scan(self) -> list[object]:
        return []


class SchedulerIterationTests(TestCase):
    def test_each_detector_invoked_once(self) -> None:
        a = _CountingDetector()
        b = _CountingDetector()
        run_tier(Tier.CHEAP, detectors=[a, b], budget=BudgetVerdict.allow())
        assert a.scan_count == 1
        assert b.scan_count == 1


@dataclass(slots=True)
class _StubWithRerender:
    """A detector that emits one report and carries its own ``rerender`` self-heal."""

    name: ClassVar[str] = "stub_rerender"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "info"
    max_rung: ClassVar[str] = ActionRung.STATUSLINE
    auto_fix: ClassVar[bool] = True

    rerender: object = None

    def detect(self) -> list[DetectorReport]:
        return [
            DetectorReport(
                detector="stub_rerender",
                dedup_key="stub_rerender::x",
                state_hash="h1",
                severity="info",
                max_rung=ActionRung.STATUSLINE,
                summary="stub",
                auto_fix=True,
            )
        ]

    def scan(self) -> list[object]:
        return []


class SchedulerAutoFixAdapterTests(TestCase):
    """``_detector_auto_fix`` adapts a detector's own ``rerender`` into the ladder callable (#2625)."""

    def test_adapter_routes_a_detectors_own_rerender(self) -> None:
        from teatree.loop.self_improve.detectors import StaleStatuslineEntryDetector  # noqa: PLC0415
        from teatree.loop.self_improve.schedule import _detector_auto_fix  # noqa: PLC0415

        rerender = MagicMock()
        adapted = _detector_auto_fix(StaleStatuslineEntryDetector(rerender=rerender))

        assert adapted is not None
        adapted(object())
        rerender.assert_called_once()

    def test_adapter_returns_none_for_a_detector_without_rerender(self) -> None:
        from teatree.loop.self_improve.detectors import DispatchGapDetector  # noqa: PLC0415
        from teatree.loop.self_improve.schedule import _detector_auto_fix  # noqa: PLC0415

        assert _detector_auto_fix(DispatchGapDetector()) is None

    def test_run_tier_routes_per_detector_rerender_when_no_global_callable(self) -> None:
        """Without a global ``auto_fix_callable``, the ladder gets the detector's own rerender.

        The fallback for a directly-constructed detector with no injected seam.
        Both live orchestration entry points (the dedicated ``loop_self_improve``
        slot and the tick piggyback) inject the real seam as the global callable
        instead — covered by ``test_explicit_global_callable_wins_over_per_detector``.
        """
        from teatree.loop.self_improve import schedule  # noqa: PLC0415

        captured: dict[str, Callable[[DetectorReport], None] | None] = {}

        def _fake_ladder(
            report: DetectorReport,
            *,
            messaging: object = None,
            auto_fix_callable: Callable[[DetectorReport], None] | None = None,
        ) -> None:
            captured["callable"] = auto_fix_callable

        rerender = MagicMock()
        detector = _StubWithRerender(rerender=rerender)
        with patch.object(schedule, "run_action_ladder", _fake_ladder):
            run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        routed = captured["callable"]
        assert routed is not None
        routed(object())
        rerender.assert_called_once()

    def test_explicit_global_callable_wins_over_per_detector(self) -> None:
        """A piggyback-style global ``auto_fix_callable`` takes precedence over the adapter."""
        from teatree.loop.self_improve import schedule  # noqa: PLC0415

        captured: dict[str, object] = {}

        def _fake_ladder(report: DetectorReport, *, messaging: object = None, auto_fix_callable: object = None) -> None:
            captured["callable"] = auto_fix_callable

        sentinel = MagicMock()
        detector = _StubWithRerender(rerender=MagicMock())
        with patch.object(schedule, "run_action_ladder", _fake_ladder):
            run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), auto_fix_callable=sentinel)

        assert captured["callable"] is sentinel
