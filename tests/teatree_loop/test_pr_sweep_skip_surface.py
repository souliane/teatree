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
from teatree.core.models import BotPing, SweepSkipStreak
from teatree.core.notify_ledger import already_sent_noop
from teatree.loop import pr_sweep_skip_surface
from teatree.loop.pr_sweep_skip_surface import REANNOUNCE_COOLDOWN, SURFACE_AFTER_TICKS, record_sweep_outcomes
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


def _blocked(*, pr_id: int = 7, reason: str = "keystone_refused", slug: str = "o/r") -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.blocked",
        summary=f"{slug}#{pr_id} blocked ({reason})",
        payload={
            "slug": slug,
            "pr_id": pr_id,
            "decision": "blocked",
            "reason": reason,
            "merged": False,
            "overlay": "t3",
            "url": f"https://example.test/{slug}/pull/{pr_id}",
        },
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

    def test_a_new_reason_within_the_cooldown_does_not_re_arm(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="ci_pending")], notify=notifier)
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="changes_requested")], notify=notifier)

        assert len(notifier.sent) == 1

    def test_a_still_stuck_pr_re_arms_once_the_cooldown_has_elapsed(self) -> None:
        notifier = _Recorder()
        start = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="ci_pending")], notify=notifier, now=start)
        later = start + REANNOUNCE_COOLDOWN + dt.timedelta(minutes=1)
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="changes_requested")], notify=notifier, now=later)

        assert len(notifier.sent) == 2
        assert "changes_requested" in notifier.sent[1][0]

    def test_a_flapping_ci_verdict_still_reaches_the_threshold(self) -> None:
        """The flappiest PRs are the ones the operator most needs told about (#4095)."""
        notifier = _Recorder()
        flapping = ["ci_pending", "ci_red", "required_checks_indeterminate", "ci_red", "uv_audit_red_but_clean_on_main"]

        for reason in flapping:
            record_sweep_outcomes([_skip(reason=reason)], notify=notifier)

        assert len(notifier.sent) == 1
        assert f"{SURFACE_AFTER_TICKS} consecutive" in notifier.sent[0][0]

    def test_a_pr_blocked_every_pass_ages_into_an_announcement(self) -> None:
        # A persistent refusal rides ``decision="blocked"``, not ``skip`` — the
        # shape ``keystone_refused`` always takes. Treating it as a resolution
        # erased the streak every pass, so the PR could never age into a DM.
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_blocked()], notify=notifier)

        assert len(notifier.sent) == 1
        assert "keystone_refused" in notifier.sent[0][0]
        assert SweepSkipStreak.objects.get(slug="o/r", pr_id=7).tick_count == SURFACE_AFTER_TICKS

    def test_the_idempotency_key_pins_the_pr_and_the_cooldown_window(self) -> None:
        notifier = _Recorder()
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip()], notify=notifier, now=moment)

        assert notifier.sent[0][1] == "pr_sweep_aged_skip:o/r#7:20455"


class TestReminderSurvivesTheNotifyLedger(django.test.TestCase):
    """A reminder the notify ledger has already sent under that key is no reminder at all.

    ``already_sent_noop`` no-ops a delivered key forever, so keying the announcement on
    the skip REASON swallowed the backed-off reminder for the dominant case — a PR stuck
    on one unchanged reason for days — while a reason wobble sailed through. That is the
    exact inversion of the intended backoff.
    """

    def _stuck_on(self, reason: str, notifier: _Recorder, moment: dt.datetime) -> None:
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason=reason)], notify=notifier, now=moment)

    def test_a_same_reason_reminder_uses_a_key_the_ledger_has_not_already_sent(self) -> None:
        notifier = _Recorder()
        start = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        self._stuck_on("ci_red", notifier, start)
        text, key = notifier.sent[0]
        BotPing.objects.create(idempotency_key=key, kind=BotPing.Kind.INFO, status=BotPing.Status.SENT, text=text)
        self._stuck_on("ci_red", notifier, start + REANNOUNCE_COOLDOWN + dt.timedelta(minutes=1))

        assert len(notifier.sent) == 2
        assert already_sent_noop(notifier.sent[1][1]) is None

    def test_two_announcements_inside_one_window_share_a_key_whatever_the_reason(self) -> None:
        notifier = _Recorder()
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        self._stuck_on("ci_red", notifier, moment)
        SweepSkipStreak.objects.filter(slug="o/r", pr_id=7).update(surfaced_at=None)
        self._stuck_on("draft", notifier, moment)

        assert len(notifier.sent) == 2
        assert notifier.sent[0][1] == notifier.sent[1][1]


class TestOnlyAMergeClearsAStreak(django.test.TestCase):
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

    def test_a_merged_pr_clears_its_streak(self) -> None:
        # The guard against over-correcting M1 into a streak that never resolves:
        # a long refusal run that finally LANDS must leave no row behind, or the
        # owner is DM'd about a healthy PR and the merge brake counts it forever.
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS + 2):
            record_sweep_outcomes([_blocked()], notify=notifier)
        record_sweep_outcomes([_merged()], notify=notifier)

        assert not SweepSkipStreak.objects.filter(slug="o/r", pr_id=7).exists()

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
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        with mock.patch("teatree.messaging.notify_with_fallback") as sent:
            for _ in range(SURFACE_AFTER_TICKS):
                pr_sweep_skip_surface.record_sweep_outcomes([_skip()], now=moment)

        assert sent.call_count == 1
        assert sent.call_args.kwargs["idempotency_key"] == "pr_sweep_aged_skip:o/r#7:20455"

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
