"""Tests for :mod:`teatree.core.availability` — 24/7 dual question-mode (#58).

Mode resolution is the load-bearing piece of BLUEPRINT §17.1 invariant 9.
The precedence chain (manual override → cron-window schedule → default)
is asserted at each layer independently so a regression on any one
layer fails its own assertion rather than corrupting the others.
"""

import json
import logging
import sqlite3
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from teatree.core import availability
from teatree.core.availability import (
    LIVE_TURN_FRESHNESS,
    MODE_AUTONOMOUS_AWAY,
    MODE_AWAY,
    MODE_PRESENT,
    PRESENCE_FRESHNESS,
    VALID_MODES,
    Override,
    PresenceHeartbeat,
    Schedule,
    clear_override,
    load_override,
    resolve_mode,
    write_override,
)
from teatree.paths import DATA_DIR


@pytest.fixture
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "availability_override.json"
    monkeypatch.setattr("teatree.core.availability.override_path", lambda: target)
    return target


@pytest.fixture
def presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PresenceHeartbeat:
    target = tmp_path / "availability_presence"
    heartbeat = PresenceHeartbeat(locate=lambda: target)
    monkeypatch.setattr(availability, "PRESENCE", heartbeat)
    return heartbeat


class TestScheduleFromTable:
    def test_empty_section_yields_empty_windows(self) -> None:
        s = Schedule.from_table({})
        assert s.timezone == ""
        assert s.windows == ()

    def test_none_yields_empty_schedule(self) -> None:
        s = Schedule.from_table(None)
        assert s.windows == ()

    def test_invalid_cron_is_dropped_silently(self) -> None:
        s = Schedule.from_table(
            {
                "timezone": "Europe/Paris",
                "windows": ["0 9-16 * * 1-5", "not a cron", 42, ""],
            }
        )
        assert s.timezone == "Europe/Paris"
        assert s.windows == ("0 9-16 * * 1-5",)

    def test_multiple_valid_windows_are_kept(self) -> None:
        s = Schedule.from_table({"windows": ["0 9-12 * * 1-5", "0 14-17 * * 1-5"]})
        assert len(s.windows) == 2


class TestScheduleIsPresentAt:
    def test_empty_schedule_is_present_default(self) -> None:
        s = Schedule()
        assert s.is_present_at(datetime(2026, 5, 18, 3, 0, tzinfo=UTC)) is True

    def test_weekday_business_hours_window(self) -> None:
        # Monday 10:00 in Paris is within 09-16 Mon-Fri.
        s = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        monday_10am_paris = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)  # 10:00 in Paris (UTC+2 in May)
        assert s.is_present_at(monday_10am_paris) is True

    def test_weekend_outside_business_window_is_away(self) -> None:
        s = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        saturday_11am_paris = datetime(2026, 5, 16, 9, 0, tzinfo=UTC)
        assert s.is_present_at(saturday_11am_paris) is False

    def test_late_evening_outside_window_is_away(self) -> None:
        s = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        monday_11pm_paris = datetime(2026, 5, 18, 21, 0, tzinfo=UTC)
        assert s.is_present_at(monday_11pm_paris) is False

    # Bug A regression: off-:00 minutes inside the window must be present.
    def test_minute_cron_off_minute_inside_window_is_present(self) -> None:
        # "* 9-16 * * 1-5" fires every minute during work hours.
        # Monday 10:30 Paris (08:30 UTC in May) must be present.
        s = Schedule(timezone="Europe/Paris", windows=("* 9-16 * * 1-5",))
        monday_1030_paris = datetime(2026, 5, 18, 8, 30, tzinfo=UTC)
        assert s.is_present_at(monday_1030_paris) is True

    def test_hour_cron_off_minute_inside_window_is_present(self) -> None:
        # "0 9-16 * * 1-5" fires at HH:00 each hour from 09 to 16.
        # The window is a span -- 10:30 is inside the 10:00-10:59 slice -> present.
        s = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        monday_1030_paris = datetime(2026, 5, 18, 8, 30, tzinfo=UTC)
        assert s.is_present_at(monday_1030_paris) is True

    def test_hour_cron_end_of_last_hour_is_present(self) -> None:
        # 16:59 is still within the 16:00 slice → present.
        s = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        monday_1659_paris = datetime(2026, 5, 18, 14, 59, tzinfo=UTC)
        assert s.is_present_at(monday_1659_paris) is True

    def test_hour_cron_exactly_one_step_after_last_fire_is_away(self) -> None:
        # 17:00 is one cadence (1h) beyond the last 16:00 fire's span -> away.
        s = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        monday_1700_paris = datetime(2026, 5, 18, 15, 0, tzinfo=UTC)
        assert s.is_present_at(monday_1700_paris) is False


# A sparse cron fires less often than once an hour. Naively taking the gap to
# the previous fire as the present-span would mark the user present for the
# whole (multi-hour or multi-day) gap. A fire must present only for its own
# hour, capped at the 1h max span.
class TestScheduleSparseWindow:
    SPARSE = ("0 9 * * 1-5",)  # 09:00 weekdays only — one fire per day.
    TWICE = ("0 9,17 * * 1-5",)  # 09:00 and 17:00 weekdays — two fires per day.

    def test_single_daily_fire_present_within_own_hour(self) -> None:
        s = Schedule(windows=self.SPARSE)
        assert s.is_present_at(datetime(2026, 5, 18, 9, 0, tzinfo=UTC)) is True
        assert s.is_present_at(datetime(2026, 5, 18, 9, 30, tzinfo=UTC)) is True
        assert s.is_present_at(datetime(2026, 5, 18, 9, 59, tzinfo=UTC)) is True

    def test_single_daily_fire_away_after_own_hour(self) -> None:
        s = Schedule(windows=self.SPARSE)
        assert s.is_present_at(datetime(2026, 5, 18, 10, 0, tzinfo=UTC)) is False

    def test_single_daily_fire_not_present_rest_of_day(self) -> None:
        # The over-present regression: with the previous backward-gap step the
        # Friday->Monday jump made these all present for up to ~3 days.
        s = Schedule(windows=self.SPARSE)
        assert s.is_present_at(datetime(2026, 5, 18, 17, 0, tzinfo=UTC)) is False
        assert s.is_present_at(datetime(2026, 5, 18, 22, 0, tzinfo=UTC)) is False
        assert s.is_present_at(datetime(2026, 5, 19, 8, 59, tzinfo=UTC)) is False

    def test_single_daily_fire_present_again_next_day(self) -> None:
        s = Schedule(windows=self.SPARSE)
        assert s.is_present_at(datetime(2026, 5, 19, 9, 0, tzinfo=UTC)) is True

    def test_two_fires_per_day_present_at_each_fire_hour(self) -> None:
        s = Schedule(windows=self.TWICE)
        assert s.is_present_at(datetime(2026, 5, 18, 9, 30, tzinfo=UTC)) is True
        assert s.is_present_at(datetime(2026, 5, 18, 17, 30, tzinfo=UTC)) is True

    def test_two_fires_per_day_away_between_fires(self) -> None:
        s = Schedule(windows=self.TWICE)
        assert s.is_present_at(datetime(2026, 5, 18, 10, 0, tzinfo=UTC)) is False
        assert s.is_present_at(datetime(2026, 5, 18, 13, 0, tzinfo=UTC)) is False
        assert s.is_present_at(datetime(2026, 5, 18, 18, 0, tzinfo=UTC)) is False

    def test_sparse_window_emits_from_table_warning(self) -> None:
        with pytest.warns(UserWarning, match="fires sparsely"):
            Schedule.from_table({"windows": ["0 9 * * 1-5"]})

    def test_span_window_emits_no_warning(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Schedule.from_table({"windows": ["* 9-16 * * 1-5", "0 9-16 * * 1-5"]})


# Bug B regression: malformed timezone must not raise — fail open to present.
class TestScheduleMalformedTimezone:
    def test_malformed_timezone_from_table_is_dropped_silently(self) -> None:
        s = Schedule.from_table({"timezone": "foo/../bar", "windows": ["* * * * *"]})
        assert s.timezone == ""

    def test_malformed_timezone_resolve_mode_does_not_raise(self) -> None:
        s = Schedule.from_table({"timezone": "foo/../bar", "windows": ["* * * * *"]})
        result = resolve_mode(schedule=s, override=None)
        assert result.mode == MODE_PRESENT

    def test_null_byte_timezone_does_not_raise(self) -> None:
        s = Schedule.from_table({"timezone": "Eur\x00ope/Paris", "windows": ["* * * * *"]})
        result = resolve_mode(schedule=s, override=None)
        assert result.mode == MODE_PRESENT


class TestOverrideRoundTrip:
    def test_write_and_load(self, override_file: Path) -> None:
        until = datetime(2030, 1, 1, tzinfo=UTC)
        write_override(MODE_AWAY, until=until)
        loaded = load_override()
        assert loaded is not None
        assert loaded.mode == MODE_AWAY
        assert loaded.until == until

    def test_clear_removes_file(self, override_file: Path) -> None:
        write_override(MODE_PRESENT)
        assert override_file.is_file()
        assert clear_override() is True
        assert override_file.exists() is False
        assert clear_override() is False

    def test_invalid_mode_raises(self, override_file: Path) -> None:
        with pytest.raises(ValueError, match="mode"):
            write_override("nope")

    def test_invalid_mode_error_names_every_valid_mode(self, override_file: Path) -> None:
        """F4.9: the rejection message enumerates all of ``VALID_MODES``, incl. autonomous_away.

        The old message hard-coded only ``'present' / 'away'``, so a user who
        mistyped ``autonomous_away`` was told the correct value was not allowed.
        """
        with pytest.raises(ValueError, match="mode") as exc_info:
            write_override("nope")
        message = str(exc_info.value)
        for valid in VALID_MODES:
            assert valid in message, f"rejection message omits the valid mode {valid!r}"

    def test_autonomous_away_round_trips(self, override_file: Path) -> None:
        """F4.9: ``autonomous_away`` is a first-class writable mode, not just a read value."""
        write_override(MODE_AUTONOMOUS_AWAY)
        loaded = load_override()
        assert loaded is not None
        assert loaded.mode == MODE_AUTONOMOUS_AWAY

    def test_load_returns_none_when_absent(self, override_file: Path) -> None:
        assert load_override() is None

    def test_load_returns_none_on_corrupt_file(self, override_file: Path) -> None:
        override_file.parent.mkdir(parents=True, exist_ok=True)
        override_file.write_text("not json", encoding="utf-8")
        assert load_override() is None

    def test_load_returns_none_on_unknown_mode(self, override_file: Path) -> None:
        override_file.parent.mkdir(parents=True, exist_ok=True)
        override_file.write_text('{"mode": "neither"}', encoding="utf-8")
        assert load_override() is None

    def test_write_uses_atomic_replace(self, override_file: Path) -> None:
        write_override(MODE_AWAY)
        # No leftover .tmp files in the directory.
        leftovers = [p for p in override_file.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


class TestResolveMode:
    def test_default_with_no_override_no_schedule_is_present(self, override_file: Path) -> None:
        resolution = resolve_mode()
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "default"

    def test_active_override_wins_over_schedule(self, override_file: Path) -> None:
        # Schedule would say away on a weekend.
        schedule = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        saturday = datetime(2026, 5, 16, 9, 0, tzinfo=UTC)
        write_override(MODE_PRESENT)
        resolution = resolve_mode(now=saturday, schedule=schedule)
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "override"

    def test_expired_override_is_ignored(self, override_file: Path) -> None:
        expired_until = datetime(2020, 1, 1, tzinfo=UTC)
        write_override(MODE_AWAY, until=expired_until)
        resolution = resolve_mode(now=datetime(2026, 5, 18, tzinfo=UTC))
        assert resolution.source != "override"

    def test_schedule_decides_when_no_override(self, override_file: Path) -> None:
        schedule = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        saturday = datetime(2026, 5, 16, 9, 0, tzinfo=UTC)
        resolution = resolve_mode(now=saturday, schedule=schedule)
        assert resolution.mode == MODE_AWAY
        assert resolution.source == "schedule"

    def test_schedule_present_in_window(self, override_file: Path) -> None:
        schedule = Schedule(timezone="Europe/Paris", windows=("0 9-16 * * 1-5",))
        monday_10 = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
        resolution = resolve_mode(now=monday_10, schedule=schedule)
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "schedule"

    def test_explicit_override_param_overrides_disk(self, override_file: Path) -> None:
        # Disk says present (override file).
        write_override(MODE_PRESENT)
        # But we pass an explicit away override that is unexpired.
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        explicit = Override(mode=MODE_AWAY, until=future)
        resolution = resolve_mode(override=explicit)
        assert resolution.mode == MODE_AWAY
        assert resolution.source == "override"

    def test_explicit_none_override_bypasses_disk_entirely(self, override_file: Path) -> None:
        write_override(MODE_AWAY)
        # Passing None means "ignore any disk override".
        resolution = resolve_mode(override=None)
        assert resolution.source != "override"


class TestPresenceHeartbeat:
    def test_record_then_load_round_trips(self, presence: PresenceHeartbeat) -> None:
        presence.record()
        loaded = presence.last_seen()
        assert loaded is not None
        assert datetime.now(tz=UTC) - loaded < timedelta(seconds=5)

    def test_load_returns_none_when_absent(self, presence: PresenceHeartbeat) -> None:
        assert presence.last_seen() is None

    def test_load_returns_none_on_corrupt_file(self, presence: PresenceHeartbeat) -> None:
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not a timestamp", encoding="utf-8")
        assert presence.last_seen() is None

    def test_load_returns_none_on_empty_file(self, presence: PresenceHeartbeat) -> None:
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("   \n", encoding="utf-8")
        assert presence.last_seen() is None

    def test_record_writes_atomically(self, presence: PresenceHeartbeat) -> None:
        target = presence.record()
        leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_record_accepts_explicit_now(self, presence: PresenceHeartbeat) -> None:
        moment = datetime(2026, 6, 2, 22, 0, tzinfo=UTC)
        presence.record(now=moment)
        assert presence.last_seen() == moment

    def test_record_naive_now_is_assumed_utc(self, presence: PresenceHeartbeat) -> None:
        naive = datetime(2026, 6, 2, 22, 0)  # noqa: DTZ001 — deliberately naive for the guard test.
        presence.record(now=naive)
        loaded = presence.last_seen()
        assert loaded == naive.replace(tzinfo=UTC)


class TestIsLiveUserTurnKillProof:
    """Mutation kill-proof for ``PresenceHeartbeat.is_live_user_turn`` (#2058).

    ``availability.py`` is a high-value mutation module whose diff-scoped
    mutmut run executes ONLY this file. Each assertion pins one mutable point
    so a mutant (guard-negation flip, comparison-operator swap, return-value
    flip, ``or``→``and``) is caught here rather than surviving.
    """

    AT = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    def test_empty_session_id_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``if not session_id`` negation flip: a stamped same-session
        # turn exists, so only the empty-id guard can produce False here.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="", now=self.AT + timedelta(seconds=1)) is False

    def test_fresh_same_session_returns_true(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``return False`` → ``return True`` flips on the guard arms
        # and the final ``<=`` → ``<``/``>``/``>=`` swaps within the window.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT + timedelta(seconds=1)) is True

    def test_no_recorded_turn_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``turn is None`` arm: nothing stamped.
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT) is False

    def test_foreign_session_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``turn.session_id != session_id`` comparison flip and the
        # ``or`` → ``and`` swap (a fresh turn exists, only the session differs).
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-b", now=self.AT + timedelta(seconds=1)) is False

    def test_at_exact_window_boundary_is_live(self, presence: PresenceHeartbeat) -> None:
        # Kills ``<=`` → ``<``: exactly at the boundary must still be live.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT + LIVE_TURN_FRESHNESS) is True

    def test_one_microsecond_past_window_is_not_live(self, presence: PresenceHeartbeat) -> None:
        # Kills ``<=`` → ``>=``/``>``: just past the boundary must defer.
        presence.record(session_id="s-a", now=self.AT)
        past = self.AT + LIVE_TURN_FRESHNESS + timedelta(microseconds=1)
        assert presence.is_live_user_turn(session_id="s-a", now=past) is False

    def test_explicit_now_is_honored_over_wall_clock(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``now or datetime.now(tz=UTC)`` default mutation: with an
        # ancient stamp, a same-instant explicit ``now`` is live; the wall clock
        # (years later) would make it stale.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT) is True


class TestRefreshLiveTurnKillProof:
    """Mutation kill-proof for ``PresenceHeartbeat.refresh_live_turn`` (#2058).

    The slide must re-stamp ONLY an already-live same-session turn and return
    whether it did. Each assertion pins a mutable point: the guard negation,
    the ``record`` call, the two return-value flips, and the ``now`` default.
    """

    AT = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    def test_live_turn_is_restamped_to_now_and_returns_true(self, presence: PresenceHeartbeat) -> None:
        # Kills the dropped ``self.record(...)`` (the stamp must move to ``now``)
        # and the ``return True`` → ``return False`` flip.
        presence.record(session_id="s-a", now=self.AT)
        slid_to = self.AT + timedelta(seconds=30)
        assert presence.refresh_live_turn(session_id="s-a", now=slid_to) is True
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == slid_to
        assert turn.session_id == "s-a"

    def test_not_live_turn_is_a_noop_and_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``if not self.is_live_user_turn`` negation flip and the
        # ``return False`` → ``return True`` flip: nothing stamped → no-op.
        assert presence.refresh_live_turn(session_id="s-loop", now=self.AT) is False
        assert presence.last_user_turn() is None

    def test_stale_turn_is_not_revived(self, presence: PresenceHeartbeat) -> None:
        # Kills the guard flip on the stale path: a turn aged past the window
        # must NOT be re-stamped (the original stamp is unchanged).
        presence.record(session_id="s-a", now=self.AT)
        stale = self.AT + LIVE_TURN_FRESHNESS + timedelta(seconds=1)
        assert presence.refresh_live_turn(session_id="s-a", now=stale) is False
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == self.AT

    def test_foreign_session_is_not_restamped(self, presence: PresenceHeartbeat) -> None:
        # Kills the guard's session arm reaching the slide: a foreign session
        # must not move the recorded stamp or change its session.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.refresh_live_turn(session_id="s-b", now=self.AT + timedelta(seconds=5)) is False
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.session_id == "s-a"
        assert turn.at == self.AT

    def test_explicit_now_drives_the_restamp_value(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``now or datetime.now(tz=UTC)`` default mutation: the stamp
        # lands at the explicit ``now``, not the wall clock.
        presence.record(session_id="s-a", now=self.AT)
        explicit = self.AT + timedelta(seconds=10)
        presence.refresh_live_turn(session_id="s-a", now=explicit)
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == explicit


class TestLivePresenceOverridesScheduleAway:
    SCHEDULE = Schedule(timezone="UTC", windows=("* 9-16 * * 1-5",))
    # Tuesday 22:00 UTC — outside the 09-16 work window, so the schedule
    # alone would resolve to away. The bug: a user actively typing here was
    # silently muted and their AskUserQuestion calls deferred.
    EVENING = datetime(2026, 6, 2, 22, 0, tzinfo=UTC)

    def test_recent_prompt_beats_schedule_away(self, presence: PresenceHeartbeat) -> None:
        presence.record(now=self.EVENING - timedelta(minutes=2))
        resolution = resolve_mode(now=self.EVENING, schedule=self.SCHEDULE, override=None)
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "live"

    def test_stale_prompt_does_not_beat_schedule_away(self, presence: PresenceHeartbeat) -> None:
        presence.record(now=self.EVENING - PRESENCE_FRESHNESS - timedelta(minutes=1))
        resolution = resolve_mode(now=self.EVENING, schedule=self.SCHEDULE, override=None)
        assert resolution.mode == MODE_AWAY
        assert resolution.source == "schedule"

    def test_no_presence_signal_leaves_schedule_away(self, presence: PresenceHeartbeat) -> None:
        resolution = resolve_mode(now=self.EVENING, schedule=self.SCHEDULE, override=None)
        assert resolution.mode == MODE_AWAY
        assert resolution.source == "schedule"

    def test_live_presence_does_not_change_schedule_present(self, presence: PresenceHeartbeat) -> None:
        # Inside the window the schedule already says present; live presence
        # must not relabel the source (the schedule decided correctly).
        presence.record(now=self.EVENING)
        monday_10 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
        resolution = resolve_mode(now=monday_10, schedule=self.SCHEDULE, override=None)
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "schedule"

    def test_explicit_away_override_beats_live_presence(self, override_file: Path, presence: PresenceHeartbeat) -> None:
        # A deliberate holiday `away` override is authoritative even when the
        # user walks up and types — the override expresses explicit intent.
        future = self.EVENING + timedelta(hours=2)
        write_override(MODE_AWAY, until=future)
        presence.record(now=self.EVENING - timedelta(minutes=1))
        resolution = resolve_mode(now=self.EVENING, schedule=self.SCHEDULE)
        assert resolution.mode == MODE_AWAY
        assert resolution.source == "override"

    def test_explicit_presence_param_overrides_disk(self, presence: PresenceHeartbeat) -> None:
        # The disk heartbeat is stale, but an explicit fresh presence param
        # still upgrades — each layer is independently testable.
        presence.record(now=self.EVENING - PRESENCE_FRESHNESS - timedelta(hours=1))
        resolution = resolve_mode(
            now=self.EVENING,
            schedule=self.SCHEDULE,
            override=None,
            presence=self.EVENING - timedelta(minutes=1),
        )
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "live"

    def test_explicit_none_presence_ignores_disk(self, presence: PresenceHeartbeat) -> None:
        # Passing presence=None means "ignore any disk heartbeat".
        presence.record(now=self.EVENING - timedelta(minutes=1))
        resolution = resolve_mode(now=self.EVENING, schedule=self.SCHEDULE, override=None, presence=None)
        assert resolution.mode == MODE_AWAY
        assert resolution.source == "schedule"

    def test_live_presence_irrelevant_with_no_schedule(self, presence: PresenceHeartbeat) -> None:
        # No windows -> default present already; presence does not relabel.
        presence.record(now=self.EVENING)
        resolution = resolve_mode(now=self.EVENING, schedule=Schedule(), override=None)
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "default"


# ---------------------------------------------------------------------------
# Mutation kill-proofs (#44). ``availability.py`` is a high-value safety module
# whose diff-scoped mutmut run mutates ONLY this file. Each assertion below pins
# one mutable point so a specific mutant (guard flip, comparison swap, wrong
# section/env/key name, dropped or non-forwarded argument, naive-datetime
# normalisation, atomic-write contract) is caught here rather than surviving.
# Verified against a Linux-container mutmut run (mutmut fork-segfaults on macOS).
# ---------------------------------------------------------------------------

WINDOW = "* 9-16 * * 1-5"  # per-minute business-hours window: valid, non-sparse.


class TestDurableFilePaths:
    """``override_path`` / ``presence_path`` resolve to their exact DATA_DIR files."""

    def test_override_path_is_data_dir_json(self) -> None:
        # Kills the ``/`` -> ``*`` operator swap (raises TypeError when called)
        # and the filename XX-wrap / upper-case mutations.
        assert availability.override_path() == DATA_DIR / "availability_override.json"

    def test_presence_path_is_data_dir_presence(self) -> None:
        assert availability.presence_path() == DATA_DIR / "availability_presence"


def _seed_schedule(db: Path, value: object) -> None:
    """Seed the DB-home ``availability_schedule`` setting the cold reader resolves."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            ("", "availability_schedule", json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


class TestLoadSchedule:
    """``load_schedule`` reads the DB-home ``availability_schedule`` setting."""

    def test_reads_timezone_and_windows_from_db(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _seed_schedule(db, {"timezone": "Europe/Paris", "windows": [WINDOW]})
        schedule = availability.load_schedule(db_path=db)
        assert schedule.timezone == "Europe/Paris"
        assert schedule.windows == (WINDOW,)

    def test_absent_db_returns_empty_schedule(self, tmp_path: Path) -> None:
        schedule = availability.load_schedule(db_path=tmp_path / "absent.sqlite3")
        assert schedule.windows == ()

    def test_db_without_schedule_row_is_empty(self, tmp_path: Path) -> None:
        # A DB with no ``availability_schedule`` row yields an empty schedule.
        db = tmp_path / "db.sqlite3"
        _seed_schedule(db, {"windows": [WINDOW]})
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM teatree_config_setting WHERE key='availability_schedule'")
        conn.commit()
        conn.close()
        schedule = availability.load_schedule(db_path=db)
        assert schedule.windows == ()

    def test_default_reads_canonical_db_via_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # With no db_path, load_schedule resolves the canonical DB via T3_CONFIG_DB.
        db = tmp_path / "db.sqlite3"
        _seed_schedule(db, {"windows": [WINDOW]})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        schedule = availability.load_schedule()
        assert schedule.windows == (WINDOW,)


class TestLoadOverrideUntilNormalization:
    """A naive ``until`` on disk is normalised to a UTC-aware datetime on load."""

    def test_naive_until_is_made_utc_aware(self, override_file: Path) -> None:
        # Kills the ``tzinfo is None`` guard flip, the ``until = None`` drop, and
        # ``replace(tzinfo=None)``: a naive ISO ``until`` must load as UTC-aware.
        override_file.parent.mkdir(parents=True, exist_ok=True)
        override_file.write_text('{"mode": "away", "until": "2030-01-01T00:00:00"}', encoding="utf-8")
        loaded = load_override()
        assert loaded is not None
        assert loaded.until == datetime(2030, 1, 1, tzinfo=UTC)
        assert loaded.until.tzinfo is not None

    def test_reads_the_override_as_utf8(self, override_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills the ``read_text(encoding="utf-8")`` mutants (None / "UTF-8"): the
        # override file is read under an explicit utf-8 codec.
        override_file.parent.mkdir(parents=True, exist_ok=True)
        override_file.write_text('{"mode": "away"}', encoding="utf-8")
        real_read = Path.read_text
        seen: list[dict[str, object]] = []

        def spy_read(self: Path, **kwargs: object) -> str:
            seen.append(kwargs)
            return real_read(self, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy_read)
        load_override()
        assert seen == [{"encoding": "utf-8"}]


class TestResolveModePresenceBoundary:
    """Live presence exactly at the freshness boundary upgrades away -> present."""

    def test_presence_exactly_at_freshness_is_live(self) -> None:
        # Kills the final ``<=`` -> ``<`` swap in the live-presence branch: a
        # prompt exactly PRESENCE_FRESHNESS old is still live.
        schedule = Schedule(timezone="UTC", windows=("* 9-16 * * 1-5",))
        evening = datetime(2026, 6, 2, 22, 0, tzinfo=UTC)  # outside the window
        resolution = resolve_mode(
            now=evening,
            schedule=schedule,
            override=None,
            presence=evening - PRESENCE_FRESHNESS,
        )
        assert resolution.mode == MODE_PRESENT
        assert resolution.source == "live"


class TestWriteOverrideDrainOnReturn:
    """Setting present from a deferring mode fires the deferred-question drain."""

    @pytest.fixture
    def capture_drain(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
        calls: list[dict[str, str]] = []

        def fake_drain(**kwargs: str) -> tuple[int, int]:
            calls.append(kwargs)
            return (0, 0)

        monkeypatch.setattr(availability, "drain_deferred_questions", fake_drain)
        return calls

    def test_away_to_present_forwards_user_id_and_overlay(
        self, override_file: Path, capture_drain: list[dict[str, str]]
    ) -> None:
        # Kills prior_mode=None, the ``mode == PRESENT`` / ``prior in DEFERRING``
        # flips, and the ``user_id=None`` / ``overlay=None`` / dropped-kwarg
        # mutants in both write_override and _drain_on_return.
        write_override(MODE_AWAY)
        write_override(MODE_PRESENT, user_id="u1", overlay="ov1")
        assert capture_drain == [{"user_id": "u1", "overlay": "ov1"}]

    def test_away_to_present_defaults_are_empty_strings(
        self, override_file: Path, capture_drain: list[dict[str, str]]
    ) -> None:
        # Kills the ``user_id: str = ""`` / ``overlay: str = ""`` default-value
        # mutations (-> "XXXX").
        write_override(MODE_AWAY)
        write_override(MODE_PRESENT)
        assert capture_drain == [{"user_id": "", "overlay": ""}]

    def test_present_to_present_does_not_drain(
        self, override_file: Path, monkeypatch: pytest.MonkeyPatch, capture_drain: list[dict[str, str]]
    ) -> None:
        # Kills the ``prior in DEFERRING`` -> ``not in`` flip: a present->present
        # transition must not drain.
        monkeypatch.setenv("TEATREE_TOML", str(override_file.parent / "no-such.toml"))
        write_override(MODE_PRESENT)
        write_override(MODE_PRESENT)
        assert capture_drain == []

    def test_drain_failure_is_swallowed_and_logged(
        self, override_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Kills the ``logger.warning`` message/arg mutations and proves the flip
        # is fail-open (write_override still returns).
        monkeypatch.setattr(
            availability,
            "drain_deferred_questions",
            mock.MagicMock(side_effect=RuntimeError("slack down")),
        )
        write_override(MODE_AWAY)
        with caplog.at_level(logging.WARNING, logger="teatree.core.availability"):
            write_override(MODE_PRESENT, user_id="u1")
        messages = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
        assert messages == ["away→present auto-drain failed: slack down"]


class TestWriteOverrideDurability:
    """``write_override`` normalises ``until``, creates parents, writes atomically."""

    def test_naive_until_written_as_utc_aware(self, override_file: Path) -> None:
        # Kills the write-path ``tzinfo is None`` flip, the ``until = None`` drop,
        # and ``replace(tzinfo=None)``: the on-disk ``until`` carries a UTC offset.
        write_override(MODE_AWAY, until=datetime(2030, 1, 1, 0, 0))  # noqa: DTZ001 — deliberately naive until
        doc = json.loads(override_file.read_text(encoding="utf-8"))
        assert doc["until"] == "2030-01-01T00:00:00+00:00"

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        # Kills the ``mkdir(parents=...)`` -> False/None/dropped mutants.
        target = tmp_path / "deep" / "nested" / "override.json"
        write_override(MODE_AWAY, path=target)
        assert target.is_file()

    def test_atomic_write_uses_named_temp_utf8_sorted(
        self, override_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Kills the mkstemp prefix/suffix/dir mutants, the fdopen ``encoding``
        # mutants, and the json.dump ``sort_keys`` mutants — one atomic-write
        # contract (crash-safe temp in the target dir, utf-8, deterministic).
        mkstemp = mock.MagicMock(wraps=availability.tempfile.mkstemp)
        fdopen = mock.MagicMock(wraps=availability.os.fdopen)
        dump = mock.MagicMock(wraps=availability.json.dump)
        monkeypatch.setattr(availability.tempfile, "mkstemp", mkstemp)
        monkeypatch.setattr(availability.os, "fdopen", fdopen)
        monkeypatch.setattr(availability.json, "dump", dump)
        write_override(MODE_AWAY, path=override_file)
        mkstemp.assert_called_once_with(prefix=".override-", suffix=".tmp", dir=str(override_file.parent))
        assert fdopen.call_args.kwargs.get("encoding") == "utf-8"
        assert dump.call_args.kwargs.get("sort_keys") is True

    def test_temp_is_unlinked_when_write_fails(self, override_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills the cleanup-path ``unlink(missing_ok=True)`` -> False/None mutants:
        # a failed write cleans its temp with a fail-open unlink.
        monkeypatch.setattr(availability.json, "dump", mock.MagicMock(side_effect=RuntimeError("boom")))
        real_unlink = Path.unlink
        seen: list[dict[str, object]] = []

        def spy_unlink(self: Path, **kwargs: object) -> None:
            seen.append(kwargs)
            return real_unlink(self, **kwargs)

        monkeypatch.setattr(Path, "unlink", spy_unlink)
        with pytest.raises(RuntimeError):
            write_override(MODE_AWAY, path=override_file)
        assert {"missing_ok": True} in seen


class TestPresenceRecordDurability:
    """``PresenceHeartbeat.record`` writes a UTC-aware, atomic, utf-8 heartbeat."""

    AT = datetime(2026, 6, 2, 22, 0, tzinfo=UTC)

    def test_naive_now_written_as_utc_aware(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``moment.tzinfo is None`` flip and ``replace(tzinfo=None)``:
        # a naive ``now`` is stamped with a UTC offset on disk. (The read path
        # re-normalises, so this must be observed on the raw file.)
        target = presence.record(now=datetime(2026, 6, 2, 22, 0))  # noqa: DTZ001 — deliberately naive now
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["at"] == "2026-06-02T22:00:00+00:00"

    def test_default_session_id_is_empty(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``session_id: str = ""`` default mutation (-> "XXXX").
        target = presence.record(now=self.AT)
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["session"] == ""

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        # Kills the record ``mkdir(parents=...)`` mutants.
        target = tmp_path / "deep" / "nested" / "presence"
        PresenceHeartbeat(locate=lambda: target).record(now=self.AT)
        assert target.is_file()

    def test_atomic_write_uses_named_temp_utf8_sorted(
        self, presence: PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Kills the record mkstemp prefix/suffix/dir, fdopen encoding, and
        # json.dump sort_keys mutants.
        mkstemp = mock.MagicMock(wraps=availability.tempfile.mkstemp)
        fdopen = mock.MagicMock(wraps=availability.os.fdopen)
        dump = mock.MagicMock(wraps=availability.json.dump)
        monkeypatch.setattr(availability.tempfile, "mkstemp", mkstemp)
        monkeypatch.setattr(availability.os, "fdopen", fdopen)
        monkeypatch.setattr(availability.json, "dump", dump)
        target = presence.record(now=self.AT)
        mkstemp.assert_called_once_with(prefix=".presence-", suffix=".tmp", dir=str(target.parent))
        assert fdopen.call_args.kwargs.get("encoding") == "utf-8"
        assert dump.call_args.kwargs.get("sort_keys") is True

    def test_temp_is_unlinked_when_write_fails(
        self, presence: PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Kills the record cleanup-path ``unlink(missing_ok=True)`` mutants.
        monkeypatch.setattr(availability.json, "dump", mock.MagicMock(side_effect=RuntimeError("boom")))
        real_unlink = Path.unlink
        seen: list[dict[str, object]] = []

        def spy_unlink(self: Path, **kwargs: object) -> None:
            seen.append(kwargs)
            return real_unlink(self, **kwargs)

        monkeypatch.setattr(Path, "unlink", spy_unlink)
        with pytest.raises(RuntimeError):
            presence.record(now=self.AT)
        assert {"missing_ok": True} in seen


class TestLastUserTurnNormalization:
    """A legacy naive plain-ISO heartbeat is read back as a UTC-aware turn."""

    def test_legacy_naive_timestamp_is_made_utc_aware(self, presence: PresenceHeartbeat) -> None:
        # Kills the read-path ``at.tzinfo is None`` flip, the ``at = None`` drop,
        # and ``replace(tzinfo=None)``.
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("2026-06-02T22:00:00", encoding="utf-8")  # legacy naive ISO
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == datetime(2026, 6, 2, 22, 0, tzinfo=UTC)
        assert turn.at.tzinfo is not None

    def test_reads_the_heartbeat_as_utf8(self, presence: PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills the ``read_text(encoding="utf-8")`` mutants: a non-ASCII session
        # id round-trips only under an explicit utf-8 read.
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        real_read = Path.read_text
        seen: list[dict[str, object]] = []

        def spy_read(self: Path, **kwargs: object) -> str:
            seen.append(kwargs)
            return real_read(self, **kwargs)

        target.write_text('{"at": "2026-06-02T22:00:00+00:00", "session": "s-a"}', encoding="utf-8")
        monkeypatch.setattr(Path, "read_text", spy_read)
        presence.last_user_turn()
        assert seen == [{"encoding": "utf-8"}]


class TestLiveTurnWallClockDefault:
    """``is_live_user_turn`` / ``refresh_live_turn`` default ``now`` to a UTC clock."""

    def test_is_live_user_turn_defaults_now_to_utc(self, presence: PresenceHeartbeat) -> None:
        # Kills ``datetime.now(tz=UTC)`` -> ``tz=None``: a naive wall clock would
        # raise on ``naive - aware`` when comparing against the aware stamp.
        presence.record(session_id="s-a", now=datetime.now(tz=UTC))
        assert presence.is_live_user_turn(session_id="s-a") is True

    def test_refresh_live_turn_defaults_now_to_utc(self, presence: PresenceHeartbeat) -> None:
        presence.record(session_id="s-a", now=datetime.now(tz=UTC))
        assert presence.refresh_live_turn(session_id="s-a") is True


class TestCronAnchorDeterminism:
    """The sparse-window probe anchors cadence at the fixed deterministic epoch."""

    def test_cron_cadence_forwards_its_anchor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills the ``croniter(expr, anchor)`` -> ``None`` / dropped-arg mutants.
        anchor = datetime(2011, 3, 7, 9, 30, tzinfo=UTC)
        spy = mock.MagicMock(wraps=availability.croniter)
        monkeypatch.setattr(availability, "croniter", spy)
        availability._cron_cadence("*/5 * * * *", anchor)
        assert spy.call_args_list[0] == mock.call("*/5 * * * *", anchor)

    def test_is_sparse_window_anchors_at_fixed_epoch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills the ``datetime(2000, 1, 1, tzinfo=UTC)`` anchor mutations (year,
        # month, day, tzinfo, None) that would make sparseness non-deterministic.
        captured: list[object] = []

        def fake_cadence(expr: str, anchor: object) -> timedelta:
            captured.append(anchor)
            return timedelta(hours=2)

        monkeypatch.setattr(availability, "_cron_cadence", fake_cadence)
        assert availability._is_sparse_window("0 9 * * 1-5") is True
        assert captured == [datetime(2000, 1, 1, tzinfo=UTC)]


class TestPendingQuestions:
    """``pending_questions_count`` / ``iter_pending_questions`` honour ``using``."""

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_count_reflects_pending_rows(self) -> None:
        assert availability.pending_questions_count() == 0
        availability.DeferredQuestion.record("q1")
        availability.DeferredQuestion.record("q2")
        assert availability.pending_questions_count() == 2

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_count_forwards_using(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills ``pending(using=using)`` -> ``using=None``: the caller's DB alias
        # must be forwarded, not silently replaced.
        seen: list[str | None] = []
        real_pending = availability.DeferredQuestion.pending.__func__

        def spy(cls: type, *, using: str | None = None) -> object:
            seen.append(using)
            return real_pending(cls, using=using)

        monkeypatch.setattr(availability.DeferredQuestion, "pending", classmethod(spy))
        availability.DeferredQuestion.record("q1")
        assert availability.pending_questions_count(using="default") == 1
        assert seen == ["default"]

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_iter_forwards_using(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str | None] = []
        real_pending = availability.DeferredQuestion.pending.__func__

        def spy(cls: type, *, using: str | None = None) -> object:
            seen.append(using)
            return real_pending(cls, using=using)

        monkeypatch.setattr(availability.DeferredQuestion, "pending", classmethod(spy))
        availability.DeferredQuestion.record("q1")
        assert len(list(availability.iter_pending_questions(using="default"))) == 1
        assert seen == ["default"]
