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
from teatree.core.models import BotPing, PullRequest, SkipObservation, SweepSkipStreak, Ticket
from teatree.core.notify_ledger import already_sent_noop
from teatree.loop import pr_sweep_skip_surface
from teatree.loop.pr_sweep_skip_surface import REANNOUNCE_COOLDOWN, SURFACE_AFTER_TICKS, record_sweep_outcomes
from teatree.loop.scanners.base import ScanSignal


def _skip(*, pr_id: int = 7, reason: str = "ci_pending", slug: str = "o/r", url: str | None = None) -> ScanSignal:
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
            "url": f"https://example.test/{slug}/pull/{pr_id}" if url is None else url,
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
        # The second reason must be one that announces at all — a park is covered by
        # TestADraftNeverPages; this pins the KEY being reason-independent.
        notifier = _Recorder()
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        self._stuck_on("ci_red", notifier, moment)
        SweepSkipStreak.objects.filter(slug="o/r", pr_id=7).update(surfaced_at=None)
        self._stuck_on("changes_requested", notifier, moment)

        assert len(notifier.sent) == 2
        assert notifier.sent[0][1] == notifier.sent[1][1]


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
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        with mock.patch("teatree.messaging.notify_with_fallback") as sent:
            for _ in range(SURFACE_AFTER_TICKS):
                pr_sweep_skip_surface.record_sweep_outcomes([_skip()], now=moment)

        assert sent.call_count == 1
        assert sent.call_args.kwargs["idempotency_key"] == "pr_sweep_aged_skip:o/r#7:20455"

    def test_an_erroring_ledger_write_never_aborts_the_tick(self) -> None:
        with mock.patch.object(pr_sweep_skip_surface, "_observe", side_effect=RuntimeError("db down")):
            assert pr_sweep_skip_surface.record_sweep_outcomes([_skip()], notify=_Recorder()) == []


class TestADraftNeverPages(django.test.TestCase):
    """A draft PR is parked on purpose — no run length turns that into an owner alarm (#4523)."""

    def test_a_thousand_ticks_of_draft_send_nothing(self) -> None:
        notifier = _Recorder()
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        for tick in range(1000):
            record_sweep_outcomes(
                [_skip(reason="draft")],
                notify=notifier,
                now=moment + dt.timedelta(minutes=tick),
            )

        assert notifier.sent == []

    def test_the_park_still_stands_in_the_doctor_view(self) -> None:
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="draft")], notify=_Recorder())

        row = SweepSkipStreak.objects.get(slug="o/r", pr_id=7)
        assert (row.tick_count, row.reason) == (SURFACE_AFTER_TICKS, "draft")
        assert SweepSkipStreak.objects.standing(threshold=SURFACE_AFTER_TICKS).parks == 1


class TestEveryAlarmCarriesALink(django.test.TestCase):
    """`no URL recorded` is a defect in its own right — the reader cannot act on it (#4523)."""

    def _row(self, *, url: str = "", slug: str = "o/r") -> SweepSkipStreak:
        return SweepSkipStreak.objects.observe(SkipObservation(slug=slug, pr_id=7, reason="ci_red", url=url))

    def test_a_blank_url_is_derived_from_the_slug_and_id(self) -> None:
        assert pr_sweep_skip_surface.link_for(self._row()) == "https://github.com/o/r/pull/7"

    def test_a_recorded_url_wins_over_the_derived_one(self) -> None:
        assert pr_sweep_skip_surface.link_for(self._row(url="https://example.test/x")) == "https://example.test/x"

    def test_a_slug_that_is_not_a_repo_derives_nothing(self) -> None:
        assert pr_sweep_skip_surface.link_for(self._row(slug="fix/some-branch")) == ""

    def test_the_dm_text_never_says_no_url_recorded(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="ci_red", url="")], notify=notifier)

        text = notifier.sent[0][0]
        assert "no URL recorded" not in text
        assert "https://github.com/o/r/pull/7" in text

    def test_an_underivable_link_falls_back_to_the_ref(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(reason="ci_red", slug="fix/branch", url="")], notify=notifier)

        text = notifier.sent[0][0]
        assert "no URL recorded" not in text
        assert text.endswith("fix/branch#7")


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


def _fossil(*, slug: str = "o/r", pr_id: int = 4055, seen_ago: dt.timedelta, now: dt.datetime) -> SweepSkipStreak:
    """A streak frozen mid-flight — the shape a PR leaves behind when it stops being open."""
    return SweepSkipStreak.objects.create(
        overlay="t3",
        slug=slug,
        pr_id=pr_id,
        reason="ci_pending",
        url=f"https://example.test/{slug}/pull/{pr_id}",
        first_seen_at=now - seen_ago,
        last_seen_at=now - seen_ago,
        tick_count=57,
        surfaced_at=now - REANNOUNCE_COOLDOWN - dt.timedelta(minutes=1),
    )


def _announced(notifier: _Recorder) -> set[str]:
    return {text.split()[1] for text, _key in notifier.sent}


class TestATerminalPrIsDroppedNotAnnounced(django.test.TestCase):
    """A closed or merged PR cannot merge, so `ci_pending` on it describes nothing (#4518).

    Every assertion here is absence-satisfied on its own, so each pairs the silence with
    a live PR announced in the SAME call — a harness that announced nothing would pass
    the first half and fail the control.
    """

    def setUp(self) -> None:
        self.now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        self.notifier = _Recorder()

    def _sweep_with_a_live_control(self) -> None:
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(pr_id=99)], notify=self.notifier, now=self.now)

    def _local_pr(self, state: PullRequest.State, *, pr_id: int = 4055, repo: str = "o/r") -> None:
        ticket = Ticket.objects.create(issue_url=f"https://example.test/i/{pr_id}", overlay="t3")
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3",
            url=f"https://example.test/{repo}/pull/{pr_id}",
            repo=repo,
            iid=str(pr_id),
            state=state,
        )

    def test_a_closed_prs_aged_streak_is_dropped_while_a_live_pr_still_announces(self) -> None:
        _fossil(seen_ago=dt.timedelta(minutes=1), now=self.now)
        self._local_pr(PullRequest.State.CLOSED)

        self._sweep_with_a_live_control()

        assert _announced(self.notifier) == {"o/r#99"}
        assert not SweepSkipStreak.objects.filter(pr_id=4055).exists()

    def test_a_merged_prs_aged_streak_is_dropped(self) -> None:
        _fossil(seen_ago=dt.timedelta(minutes=1), now=self.now)
        self._local_pr(PullRequest.State.MERGED)

        self._sweep_with_a_live_control()

        assert _announced(self.notifier) == {"o/r#99"}
        assert not SweepSkipStreak.objects.filter(pr_id=4055).exists()

    def test_an_open_local_row_never_drops_the_streak(self) -> None:
        _fossil(seen_ago=dt.timedelta(minutes=1), now=self.now)
        self._local_pr(PullRequest.State.OPEN)

        self._sweep_with_a_live_control()

        assert SweepSkipStreak.objects.filter(pr_id=4055).exists()

    def test_the_terminal_match_folds_slug_case_like_the_pr_row_rule(self) -> None:
        _fossil(slug="Owner/Repo", seen_ago=dt.timedelta(minutes=1), now=self.now)
        self._local_pr(PullRequest.State.CLOSED, repo="owner/repo")

        self._sweep_with_a_live_control()

        assert not SweepSkipStreak.objects.filter(slug="Owner/Repo").exists()


class TestAPrThatLeftTheOpenSetIsDropped(django.test.TestCase):
    """The sweep enumerates OPEN PRs only, so a PR it stops seeing can no longer merge.

    This is the reported #4518 shape: PR 4055 closed with NO local ``PullRequest`` row at
    all, so its streak froze at 57 while ``age_label`` kept growing and the 24h cooldown
    re-armed the same dead finding for fifteen days.
    """

    def setUp(self) -> None:
        self.now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        self.notifier = _Recorder()

    def _sweep_with_a_live_control(self) -> None:
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip(pr_id=99)], notify=self.notifier, now=self.now)

    def test_a_fossil_with_no_local_row_is_dropped_while_a_live_pr_still_announces(self) -> None:
        _fossil(seen_ago=dt.timedelta(days=15), now=self.now)

        self._sweep_with_a_live_control()

        assert _announced(self.notifier) == {"o/r#99"}
        assert not SweepSkipStreak.objects.filter(pr_id=4055).exists()

    def test_a_row_missed_by_one_errored_evaluation_survives_and_stays_quiet(self) -> None:
        """`scan()` emits no signal for a PR whose evaluation raised — that is not a departure."""
        _fossil(seen_ago=dt.timedelta(minutes=1), now=self.now)

        self._sweep_with_a_live_control()

        assert _announced(self.notifier) == {"o/r#99"}
        assert SweepSkipStreak.objects.filter(pr_id=4055).exists()

    def test_a_fossil_in_a_slug_this_pass_never_swept_survives(self) -> None:
        _fossil(slug="other/repo", seen_ago=dt.timedelta(days=15), now=self.now)

        self._sweep_with_a_live_control()

        assert SweepSkipStreak.objects.filter(slug="other/repo").exists()

    def test_an_unobserved_row_is_never_announced_even_before_the_grace_elapses(self) -> None:
        _fossil(seen_ago=dt.timedelta(minutes=1), now=self.now)

        self._sweep_with_a_live_control()

        assert _announced(self.notifier) == {"o/r#99"}


class TestTheAlarmCarriesThePrUrl(django.test.TestCase):
    def test_the_dm_links_the_pr_it_pages_about(self) -> None:
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([_skip()], notify=notifier)

        text, _key = notifier.sent[0]
        assert "https://example.test/o/r/pull/7" in text
        assert "no URL recorded" not in text

    def test_a_url_less_legacy_row_derives_a_link_never_a_dead_literal(self) -> None:
        # "changes_requested", not "draft" — a draft never announces at all (#4523),
        # and this test's own point is the fallback rendering, orthogonal to the reason.
        # "o/r" looks like a real owner/repo, so link_for() derives a clickable URL
        # (#4523) rather than the bare ref TestEveryAlarmCarriesALink's branch-shaped-slug
        # case falls back to.
        urlless = ScanSignal(
            kind="pr_sweep.skip",
            summary="o/r#7 skip (changes_requested)",
            payload={"slug": "o/r", "pr_id": 7, "decision": "skip", "reason": "changes_requested", "overlay": "t3"},
        )
        notifier = _Recorder()
        for _ in range(SURFACE_AFTER_TICKS):
            record_sweep_outcomes([urlless], notify=notifier)

        text, _key = notifier.sent[0]
        assert "no URL recorded" not in text
        assert text.rstrip().endswith("https://github.com/o/r/pull/7")
