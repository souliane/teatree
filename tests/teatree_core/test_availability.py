"""Tests for :mod:`teatree.core.availability` — 24/7 dual question-mode (#58).

Mode resolution is the load-bearing piece of BLUEPRINT §17.1 invariant 9.
The precedence chain (manual override → cron-window schedule → default)
is asserted at each layer independently so a regression on any one
layer fails its own assertion rather than corrupting the others.
"""

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
