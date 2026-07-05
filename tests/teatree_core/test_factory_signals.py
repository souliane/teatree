"""DB-backed tests for the derived-on-read factory signals (SIG-PR-1).

Seeds real merge/review/CI/repair ledger rows via ``tests/factories.py`` and
pins each signal's good-vs-regressing bands plus the two anti-vacuity fixtures
the plan calls out: a dead ``my_prs`` recorder must yield S1
``instrumentation_gap`` (never a fabricated 100%), and a rubber-stamp review
window (≥5 merges, zero holds/findings) must yield S3 hard RED. ``created_at`` /
``started_at`` are ``auto_now_add``; backdate with ``update()`` to place rows in
or out of the trailing / baseline window.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory_signals import (
    FactorySignalsReport,
    SignalReading,
    SignalStatus,
    SignalVerdict,
    compute_factory_signals,
    defect_escape_rate,
    first_try_green_rate,
    merge_latency,
    repair_iteration_burn,
    review_catch_rate,
)
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.pr_slug_resolution import resolve_pr_repo_slug
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from tests.factories import (
    MergeAuditFactory,
    MergeClearFactory,
    RedCardSignalFactory,
    RedMrFixAttemptFactory,
    ReviewVerdictFactory,
    SessionFactory,
    TaskAttemptFactory,
    TaskFactory,
    TicketFactory,
    TicketTransitionFactory,
)


def _row(report: FactorySignalsReport, provider_id: str):
    return next(row for row in report.signals if row.provider_id == provider_id)


class FactorySignalsTestBase(TestCase):
    SLUG = "souliane/teatree"

    def setUp(self) -> None:
        self.now = timezone.now()

    def _merge(
        self,
        *,
        pr_id: int,
        days_ago: float,
        latency_hours: float = 1.0,
        reviewed: str = "",
        slug: str = SLUG,
    ) -> None:
        merged_at = self.now - timedelta(days=days_ago)
        issued_at = merged_at - timedelta(hours=latency_hours)
        # A merged CLEAR is consumed in production; set consumed_at so it is not
        # miscounted as an actionable-but-stale CLEAR by S4.
        clear = MergeClearFactory(pr_id=pr_id, slug=slug, issued_at=issued_at, consumed_at=merged_at)
        MergeAuditFactory(clear=clear, merged_at=merged_at)
        if reviewed == "hold":
            ReviewVerdictFactory(hold=True, slug=slug, pr_id=pr_id)
        elif reviewed == "clean":
            ReviewVerdictFactory(slug=slug, pr_id=pr_id)
        elif reviewed == "blocker":
            ReviewVerdictFactory(
                slug=slug,
                pr_id=pr_id,
                findings=[{"severity": "blocker", "summary": "unsafe", "file": "a.py", "line": 3}],
            )

    def _red(self, *, pr_id: int, days_ago: float, head: str = "") -> None:
        RedMrFixAttemptFactory(
            pr_url=f"https://github.com/{self.SLUG}/pull/{pr_id}",
            head_sha=head or f"{pr_id:040x}",
            dispatched_at=self.now - timedelta(days=days_ago),
        )

    def _fix_ticket(self, *, days_ago: float) -> None:
        ticket = TicketFactory(kind=Ticket.Kind.FIX)
        tr = TicketTransitionFactory(ticket=ticket)
        TicketTransition.objects.filter(pk=tr.pk).update(created_at=self.now - timedelta(days=days_ago))

    def _attempt(self, *, days_ago: float, iteration: int, exit_code: int = 0, phase: str = "coding") -> None:
        ticket = TicketFactory()
        session = SessionFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket, session=session, phase=phase)
        att = TaskAttemptFactory(task=task, iteration=iteration, exit_code=exit_code)
        TaskAttempt.objects.filter(pk=att.pk).update(started_at=self.now - timedelta(days=days_ago))


class S1FirstTryGreenTests(FactorySignalsTestBase):
    def test_dead_recorder_is_instrumentation_gap_never_fake_100(self) -> None:
        # 5 merges, ZERO RedMrFixAttempt rows anywhere: naive logic would call
        # every merge first-try-green (100%). Fail loud instead.
        for i in range(5):
            self._merge(pr_id=901 + i, days_ago=5)
        reading = first_try_green_rate(now=self.now)
        assert reading.status == SignalStatus.INSTRUMENTATION_GAP
        assert reading.status != SignalStatus.OK

    def test_alive_recorder_reports_real_rate(self) -> None:
        for i in range(5):
            self._merge(pr_id=901 + i, days_ago=5)
        self._red(pr_id=901, days_ago=5)
        self._red(pr_id=902, days_ago=5)
        reading = first_try_green_rate(now=self.now)
        assert reading.status == SignalStatus.OK
        assert reading.value == pytest.approx(0.6)
        assert reading.sample_size == 5

    def test_below_min_sample_is_insufficient(self) -> None:
        for i in range(3):
            self._merge(pr_id=901 + i, days_ago=5)
        reading = first_try_green_rate(now=self.now)
        assert reading.status == SignalStatus.INSUFFICIENT_DATA

    def test_regressing_against_baseline(self) -> None:
        # Baseline 0.9, current 0.6: a drift below baseline that stays above the
        # 0.5 hard floor is REGRESSING, not RED.
        for i in range(10):
            self._merge(pr_id=800 + i, days_ago=40)
        self._red(pr_id=800, days_ago=40)
        for i in range(5):
            self._merge(pr_id=900 + i, days_ago=5)
        self._red(pr_id=900, days_ago=5)
        self._red(pr_id=901, days_ago=5)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "first_try_green")
        assert row.reading.value == pytest.approx(0.6)
        assert row.baseline_value == pytest.approx(0.9)
        assert row.verdict == SignalVerdict.REGRESSING

    def test_below_hard_floor_is_red(self) -> None:
        for i in range(5):
            self._merge(pr_id=900 + i, days_ago=5)
        for pr_id in (900, 901, 902, 903):
            self._red(pr_id=pr_id, days_ago=5)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "first_try_green")
        assert row.reading.value == pytest.approx(0.2)
        assert row.tripped is True
        assert row.verdict == SignalVerdict.RED

    def test_workstream_slug_clear_counts_toward_first_try_green(self) -> None:
        # Dominant self-merge shape: each CLEAR carries a WORKSTREAM slug while its
        # RedMrFixAttempt / MergeAudit rows are keyed under the resolved OWNER/REPO
        # slug (the same `resolve_pr_repo_slug` the merge gate keys on). S1 must
        # resolve that owner/repo before the join, mirroring S3 — so a real
        # first_try_green_rate is computed instead of the whole sample collapsing
        # to insufficient_data because every workstream-slug CLEAR was dropped.
        for i in range(5):
            pr_id = 971 + i
            ticket = TicketFactory(issue_url=f"https://github.com/{self.SLUG}/issues/{pr_id}")
            merged_at = self.now - timedelta(days=5)
            clear = MergeClearFactory(
                ticket=ticket,
                pr_id=pr_id,
                slug=f"{pr_id}-feat-x",
                issued_at=merged_at - timedelta(hours=1),
                consumed_at=merged_at,
            )
            MergeAuditFactory(clear=clear, merged_at=merged_at)
        self._red(pr_id=971, days_ago=5)
        self._red(pr_id=972, days_ago=5)
        reading = first_try_green_rate(now=self.now)
        assert reading.status == SignalStatus.OK
        assert reading.status != SignalStatus.INSUFFICIENT_DATA
        assert reading.value == pytest.approx(0.6)
        assert reading.sample_size == 5

    def test_unresolvable_clear_is_dropped_not_fabricated_green(self) -> None:
        # A CLEAR whose owner/repo genuinely cannot be resolved is routed to
        # unmatched_slug and dropped from the denominator — never fabricated as a
        # first-try-green. The five resolvable merges (one CI-red) still yield a
        # real 0.8; the unresolvable one moves the sample from 6 to 5, not into it.
        for i in range(5):
            self._merge(pr_id=981 + i, days_ago=5)
        self._red(pr_id=981, days_ago=5)
        orphan = MergeClearFactory(
            pr_id=999,
            slug="orphan-workstream",
            issued_at=self.now - timedelta(days=5, hours=1),
            consumed_at=self.now - timedelta(days=5),
        )
        MergeAuditFactory(clear=orphan, merged_at=self.now - timedelta(days=5))

        def resolve_or_raise(clear: object) -> str:
            if getattr(clear, "pr_id", None) == 999:
                msg = "no resolvable repo"
                raise MergePreconditionError(msg)
            return resolve_pr_repo_slug(clear)

        with mock.patch(
            "teatree.core.factory_signal_queries.resolve_pr_repo_slug",
            side_effect=resolve_or_raise,
        ):
            report = compute_factory_signals(now=self.now)
        row = _row(report, "first_try_green")
        assert row.evidence["unmatched_slug"] == 1
        assert row.evidence["merges"] == 5
        assert row.reading.sample_size == 5
        assert row.reading.value == pytest.approx(0.8)
        assert row.reading.status == SignalStatus.OK


class S2DefectEscapeTests(FactorySignalsTestBase):
    def test_low_escape_is_ok(self) -> None:
        for i in range(10):
            self._merge(pr_id=700 + i, days_ago=40)
        self._fix_ticket(days_ago=5)
        reading = defect_escape_rate(now=self.now)
        assert reading.status == SignalStatus.OK
        assert reading.value == pytest.approx(0.1)

    def test_red_card_counts_toward_numerator(self) -> None:
        for i in range(10):
            self._merge(pr_id=700 + i, days_ago=40)
        RedCardSignalFactory(observed_at=self.now - timedelta(days=5))
        reading = defect_escape_rate(now=self.now)
        assert reading.value == pytest.approx(0.1)

    def test_no_preceding_merges_is_insufficient(self) -> None:
        self._fix_ticket(days_ago=5)
        reading = defect_escape_rate(now=self.now)
        assert reading.status == SignalStatus.INSUFFICIENT_DATA

    def test_regressing_when_escapes_rise(self) -> None:
        for i in range(10):
            self._merge(pr_id=600 + i, days_ago=70)
        for i in range(10):
            self._merge(pr_id=700 + i, days_ago=40)
        self._fix_ticket(days_ago=40)
        for _ in range(6):
            self._fix_ticket(days_ago=5)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "defect_escape")
        assert row.reading.value == pytest.approx(0.6)
        assert row.baseline_value == pytest.approx(0.1)
        assert row.verdict == SignalVerdict.REGRESSING


class S3ReviewCatchTests(FactorySignalsTestBase):
    def test_rubber_stamp_window_is_hard_red(self) -> None:
        for i in range(5):
            self._merge(pr_id=901 + i, days_ago=5)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "review_catch")
        assert row.reading.value == pytest.approx(0.0)
        assert row.reading.sample_size == 5
        assert row.tripped is True
        assert row.verdict == SignalVerdict.RED
        assert report.verdict == SignalVerdict.RED

    def test_hold_and_blocker_count_as_caught(self) -> None:
        self._merge(pr_id=901, days_ago=5, reviewed="hold")
        self._merge(pr_id=902, days_ago=5, reviewed="blocker")
        for i in range(3):
            self._merge(pr_id=903 + i, days_ago=5, reviewed="clean")
        reading = review_catch_rate(now=self.now)
        assert reading.status == SignalStatus.OK
        assert reading.value == pytest.approx(0.4)

    def test_healthy_catch_rate_is_ok(self) -> None:
        for i in range(5):
            self._merge(pr_id=901 + i, days_ago=5, reviewed="hold" if i < 2 else "clean")
        report = compute_factory_signals(now=self.now)
        row = _row(report, "review_catch")
        assert row.verdict == SignalVerdict.OK
        assert row.tripped is False

    def test_workstream_slug_clear_joins_owner_repo_keyed_verdict(self) -> None:
        # Dominant self-merge shape: the CLEAR carries a WORKSTREAM slug while its
        # HOLD verdict is keyed under the resolved OWNER/REPO slug (the same
        # `resolve_pr_repo_slug` the merge gate keys `ReviewVerdict.record` on).
        # S3 must resolve that owner/repo before `for_pr`, so a genuinely-held
        # lane counts as CAUGHT — never a false-RED rubber-stamp trip.
        for i in range(5):
            pr_id = 971 + i
            ticket = TicketFactory(issue_url=f"https://github.com/{self.SLUG}/issues/{pr_id}")
            merged_at = self.now - timedelta(days=5)
            clear = MergeClearFactory(
                ticket=ticket,
                pr_id=pr_id,
                slug=f"{pr_id}-feat-x",
                issued_at=merged_at - timedelta(hours=1),
                consumed_at=merged_at,
            )
            MergeAuditFactory(clear=clear, merged_at=merged_at)
            ReviewVerdictFactory(hold=True, slug=self.SLUG, pr_id=pr_id)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "review_catch")
        assert row.reading.value == pytest.approx(1.0)
        assert row.reading.sample_size == 5
        assert row.tripped is False
        assert row.verdict == SignalVerdict.OK


class S4MergeLatencyTests(FactorySignalsTestBase):
    def test_stale_actionable_clear_is_red_with_zero_merges(self) -> None:
        # An actionable, unconsumed CLEAR issued 3 days ago (>48h) and no merges:
        # a stalled merge loop must RED even in a zero-merge window.
        MergeClearFactory(issued_at=self.now - timedelta(days=3))
        report = compute_factory_signals(now=self.now)
        row = _row(report, "merge_latency")
        assert row.tripped is True
        assert row.verdict == SignalVerdict.RED
        assert row.evidence["stale_clear_hours"] > 48.0
        assert report.verdict == SignalVerdict.RED

    def test_fast_latency_is_ok(self) -> None:
        for i in range(5):
            self._merge(pr_id=901 + i, days_ago=5, latency_hours=2.0)
        reading = merge_latency(now=self.now)
        assert reading.status == SignalStatus.OK
        assert reading.value == pytest.approx(2.0)

    def test_consumed_clears_do_not_trip_stale(self) -> None:
        for i in range(5):
            self._merge(pr_id=901 + i, days_ago=5, latency_hours=1.0)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "merge_latency")
        assert row.evidence["stale_clear_hours"] == pytest.approx(0.0)
        assert row.tripped is False


class S5RepairBurnTests(FactorySignalsTestBase):
    def test_low_burn_is_ok(self) -> None:
        for _ in range(5):
            self._attempt(days_ago=5, iteration=1)
        reading = repair_iteration_burn(now=self.now)
        assert reading.status == SignalStatus.OK
        assert reading.value == pytest.approx(1.0)

    def test_failed_attempt_tracked_in_evidence(self) -> None:
        for _ in range(5):
            self._attempt(days_ago=5, iteration=1)
        self._attempt(days_ago=5, iteration=2, exit_code=1)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "repair_burn")
        assert row.evidence["failed_fraction"] > 0.0

    def test_regressing_when_iterations_rise(self) -> None:
        for _ in range(5):
            self._attempt(days_ago=40, iteration=1)
        for _ in range(5):
            self._attempt(days_ago=5, iteration=3)
        report = compute_factory_signals(now=self.now)
        row = _row(report, "repair_burn")
        assert row.reading.value == pytest.approx(3.0)
        assert row.baseline_value == pytest.approx(1.0)
        assert row.verdict == SignalVerdict.REGRESSING


class ReportShapeTests(FactorySignalsTestBase):
    def test_provider_functions_return_signal_reading(self) -> None:
        for provider in (
            first_try_green_rate,
            defect_escape_rate,
            review_catch_rate,
            merge_latency,
            repair_iteration_burn,
        ):
            reading = provider(now=self.now)
            assert isinstance(reading, SignalReading)
            assert reading.status in set(SignalStatus)

    def test_empty_factory_reports_five_signals_ok(self) -> None:
        report = compute_factory_signals(now=self.now)
        assert isinstance(report, FactorySignalsReport)
        assert len(report.signals) == 5
        # Nothing bad detected on an empty ledger; all signals insufficient.
        assert report.verdict == SignalVerdict.OK
        assert all(row.reading.status == SignalStatus.INSUFFICIENT_DATA for row in report.signals)

    def test_to_dict_carries_the_outer_loop_contract_keys(self) -> None:
        report = compute_factory_signals(now=self.now)
        payload = report.to_dict()
        assert payload["window_days"] == 28
        assert payload["verdict"] in {status.value for status in SignalVerdict}
        first = payload["signals"][0]
        for key in ("provider_id", "kind", "value", "sample_size", "window_days", "status", "red_when", "tripped"):
            assert key in first

    def test_window_days_flows_through(self) -> None:
        report = compute_factory_signals(window_days=7, now=self.now)
        assert report.window_days == 7
        assert all(row.reading.window_days == 7 for row in report.signals)

    def test_overlay_scopes_merges(self) -> None:
        # A merge in another overlay must not count toward this overlay's window.
        for i in range(5):
            merged_at = self.now - timedelta(days=5)
            other_ticket = TicketFactory(overlay="other-overlay")
            clear = MergeClearFactory(
                ticket=other_ticket,
                pr_id=500 + i,
                issued_at=merged_at - timedelta(hours=1),
                consumed_at=merged_at,
            )
            MergeAuditFactory(clear=clear, merged_at=merged_at)
        reading = review_catch_rate(overlay="t3-teatree", now=self.now)
        assert reading.status == SignalStatus.INSUFFICIENT_DATA
