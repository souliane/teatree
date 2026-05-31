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

from teatree.core.availability import (
    MODE_AWAY,
    MODE_PRESENT,
    Override,
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
