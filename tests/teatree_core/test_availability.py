"""Tests for :mod:`teatree.core.availability` — 24/7 dual question-mode (#58).

Mode resolution is the load-bearing piece of BLUEPRINT §17.1 invariant 9.
The precedence chain (manual override → cron-window schedule → default)
is asserted at each layer independently so a regression on any one
layer fails its own assertion rather than corrupting the others.
"""

import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from teatree.core import availability
from teatree.core.availability import (
    LIVE_TURN_FRESHNESS,
    MODE_AWAY,
    MODE_PRESENT,
    PRESENCE_FRESHNESS,
    Override,
    PresenceHeartbeat,
    Schedule,
    clear_override,
    load_override,
    resolve_mode,
    write_override,
)


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


class TestScheduleFromToml:
    def test_empty_section_yields_empty_windows(self) -> None:
        s = Schedule.from_toml({})
        assert s.timezone == ""
        assert s.windows == ()

    def test_none_yields_empty_schedule(self) -> None:
        s = Schedule.from_toml(None)
        assert s.windows == ()

    def test_invalid_cron_is_dropped_silently(self) -> None:
        s = Schedule.from_toml(
            {
                "timezone": "Europe/Paris",
                "windows": ["0 9-16 * * 1-5", "not a cron", 42, ""],
            }
        )
        assert s.timezone == "Europe/Paris"
        assert s.windows == ("0 9-16 * * 1-5",)

    def test_multiple_valid_windows_are_kept(self) -> None:
        s = Schedule.from_toml({"windows": ["0 9-12 * * 1-5", "0 14-17 * * 1-5"]})
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

    def test_sparse_window_emits_from_toml_warning(self) -> None:
        with pytest.warns(UserWarning, match="fires sparsely"):
            Schedule.from_toml({"windows": ["0 9 * * 1-5"]})

    def test_span_window_emits_no_warning(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Schedule.from_toml({"windows": ["* 9-16 * * 1-5", "0 9-16 * * 1-5"]})


# Bug B regression: malformed timezone must not raise — fail open to present.
class TestScheduleMalformedTimezone:
    def test_malformed_timezone_from_toml_is_dropped_silently(self) -> None:
        s = Schedule.from_toml({"timezone": "foo/../bar", "windows": ["* * * * *"]})
        assert s.timezone == ""

    def test_malformed_timezone_resolve_mode_does_not_raise(self) -> None:
        s = Schedule.from_toml({"timezone": "foo/../bar", "windows": ["* * * * *"]})
        result = resolve_mode(schedule=s, override=None)
        assert result.mode == MODE_PRESENT

    def test_null_byte_timezone_does_not_raise(self) -> None:
        s = Schedule.from_toml({"timezone": "Eur\x00ope/Paris", "windows": ["* * * * *"]})
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
