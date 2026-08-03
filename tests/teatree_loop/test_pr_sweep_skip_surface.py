"""A ``pr_sweep`` skip that persists across N consecutive ticks must SURFACE, once.

The sweep's ~10 skip reasons — ``no_clear_for_head``, ``ci_pending``, ``ci_red``,
``draft``, ``changes_requested``, fork provenance … — are log-only, so a PR can sit
forever with nobody told why. Silence is the defect, not the skip: persistence
surfaces the PR, the reason and the age exactly once, and a per-tick skip stays quiet.
"""

import contextlib
import datetime as dt
import io
from unittest import mock

import django.test
from django.utils import timezone

from teatree.cli.doctor.app import _check_aged_sweep_skips
from teatree.core.models import SweepSkipStreak
from teatree.loop import pr_sweep_skip_surface
from teatree.loop.pr_sweep_skip_surface import SURFACE_AFTER_TICKS, record_sweep_outcomes
from teatree.loop.scanners.base import ScanSignal


def _skip(*, pr_id: int = 7, reason: str = "ci_pending", slug: str = "o/r") -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.skip",
        summary=f"{slug}#{pr_id} skip ({reason})",
        payload={
            "slug": slug,
            "pr_id": pr_id,
            "decision": "skip",
            "reason": reason,
            "merged": False,
            "overlay": "t3",
            "url": f"https://example.test/{slug}/pull/{pr_id}",
        },
    )


def _merged(*, pr_id: int = 7, slug: str = "o/r") -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.merged",
        summary=f"{slug}#{pr_id} merged",
        payload={"slug": slug, "pr_id": pr_id, "decision": "merged", "reason": "", "merged": True},
    )


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, *, text: str, idempotency_key: str) -> None:
        self.sent.append((text, idempotency_key))


class TestSurfacesOnPersistence(django.test.TestCase):
    def test_a_single_skip_says_nothing(self) -> None:
        notifier = _Recorder()

        record_sweep_outcomes([_skip()], notify=notifier)

        assert notifier.sent == []
        assert SweepSkipStreak.objects.get(slug="o/r", pr_id=7).tick_count == 1

    def test_the_nth_consecutive_skip_surfaces_the_pr_reason_and_age(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip()], notify=notifier)

        assert len(notifier.sent) == 1
        text, _key = notifier.sent[0]
        assert "o/r#7" in text
        assert "ci_pending" in text
        assert f"{SURFACE_AFTER_TICKS} consecutive" in text

    def test_it_surfaces_once_not_every_tick(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS + 5):
            record_sweep_outcomes([_skip()], notify=notifier)

        assert len(notifier.sent) == 1

    def test_a_new_reason_re_arms_one_more_surface(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="ci_pending")], notify=notifier)
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="changes_requested")], notify=notifier)

        assert len(notifier.sent) == 2
        assert "changes_requested" in notifier.sent[1][0]

    def test_the_idempotency_key_pins_the_pr_and_reason(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip()], notify=notifier)

        assert notifier.sent[0][1] == "pr_sweep_aged_skip:o/r#7:ci_pending"

    def test_ci_verdict_flapping_surfaces_at_most_once(self) -> None:
        """A PR whose CI toggles ci_pending <-> ci_red must not re-DM (souliane/teatree#4080)."""
        notifier = _Recorder()
        flapping = ["ci_pending", "ci_red", "ci_pending", "ci_red", "ci_red", "ci_pending"]

        for reason in flapping:
            record_sweep_outcomes([_skip(reason=reason)], notify=notifier)

        assert len(notifier.sent) == 1


class TestNonSkipOutcomesClear(django.test.TestCase):
    def test_a_merge_clears_the_streak(self) -> None:
        notifier = _Recorder()
        record_sweep_outcomes([_skip()], notify=notifier)
        record_sweep_outcomes([_merged()], notify=notifier)

        assert not SweepSkipStreak.objects.filter(slug="o/r", pr_id=7).exists()

    def test_a_cleared_streak_restarts_from_zero(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS - 1):
            record_sweep_outcomes([_skip()], notify=notifier)
        record_sweep_outcomes([_merged()], notify=notifier)
        record_sweep_outcomes([_skip()], notify=notifier)

        assert notifier.sent == []

    def test_signals_from_other_scanners_are_ignored(self) -> None:
        record_sweep_outcomes([ScanSignal(kind="workstate.drift", summary="x", payload={})], notify=_Recorder())

        assert SweepSkipStreak.objects.count() == 0


class TestCrashProof(django.test.TestCase):
    def test_a_raising_notifier_never_aborts_the_tick(self) -> None:
        def _boom(*, text: str, idempotency_key: str) -> None:
            raise RuntimeError(text + idempotency_key)

        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip()], notify=_boom)

        assert SweepSkipStreak.objects.get(slug="o/r", pr_id=7).surfaced_at is not None

    def test_a_malformed_payload_is_skipped(self) -> None:
        record_sweep_outcomes(
            [ScanSignal(kind="pr_sweep.skip", summary="x", payload={"decision": "skip"})],
            notify=_Recorder(),
        )

        assert SweepSkipStreak.objects.count() == 0


class TestDoctorSurface(django.test.TestCase):
    def test_aged_streaks_are_reported_with_pr_reason_and_age(self) -> None:
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip()], notify=_Recorder())
        SweepSkipStreak.objects.filter(slug="o/r", pr_id=7).update(
            first_seen_at=timezone.now() - dt.timedelta(hours=6),
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = _check_aged_sweep_skips()

        assert ok is False
        assert "o/r#7" in out.getvalue()
        assert "ci_pending" in out.getvalue()
        assert "6h" in out.getvalue()

    def test_quiet_when_no_streak_is_aged(self) -> None:
        record_sweep_outcomes([_skip()], notify=_Recorder())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = _check_aged_sweep_skips()

        assert ok is True
        assert out.getvalue() == ""


class TestProductionNotifyPath(django.test.TestCase):
    def test_the_default_notifier_routes_through_the_notify_egress(self) -> None:
        with mock.patch("teatree.messaging.notify_with_fallback") as sent:
            for _ in range(SURFACE_AFTER_TICKS):
                pr_sweep_skip_surface.record_sweep_outcomes([_skip()])

        assert sent.call_count == 1
        assert sent.call_args.kwargs["idempotency_key"] == "pr_sweep_aged_skip:o/r#7:ci_pending"

    def test_an_erroring_ledger_write_never_aborts_the_tick(self) -> None:
        with mock.patch.object(pr_sweep_skip_surface, "_observe", side_effect=RuntimeError("db down")):
            assert pr_sweep_skip_surface.record_sweep_outcomes([_skip()], notify=_Recorder()) == []


class TestDoctorCrashProof(django.test.TestCase):
    def test_a_raising_read_degrades_to_ok(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.object(SweepSkipStreak.objects, "aged", side_effect=RuntimeError("db down")),
            contextlib.redirect_stdout(out),
        ):
            ok = _check_aged_sweep_skips()

        assert ok is True
        assert "crashed" in out.getvalue()
