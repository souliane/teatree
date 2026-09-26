# test-path: cross-cutting
"""A loop's cadence is stored once — in its row (B9 level 1-2, the plan's Shape B).

Two daily clocks in series do not make a daily job. `db_backup` had an outer row at 86400
AND an inner 24h gate reading the newest artifact's own timestamp, so each fire had to be at
least 24h after the LAST BACKUP, not 24h after the last tick — and a tick that lands a few
minutes early skips a whole day, pushing the backup later every time. The measured drift was
about twenty minutes a day.

The fix is the shape, not the numbers: the ROW is the timer, anchored to a wall-clock time so
it cannot drift, and the inner quantity that said the same thing is deleted. A different
quantity survives — `arch_review`'s 168h is not its daily row restated, so it stays.
"""

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.seed_defaults import shipped_seed_table
from teatree.config.setting_taxonomy import SettingClass, classify


def _row(name: str) -> dict[str, object]:
    return shipped_seed_table("loops")[name]


class TestTheDailyBackupIsAnchoredNotChained:
    def test_the_row_carries_a_wall_clock_anchor(self) -> None:
        assert _row("db_backup")["daily_at"]

    def test_the_inner_daily_gate_is_retired(self) -> None:
        assert "db_backup_cadence_hours" not in ALL_KNOWN_CONFIG_SETTINGS
        assert classify("db_backup_cadence_hours").classes == {SettingClass.RETIRED}


class TestTheWeeklyEvalRunsWeeklyRatherThanCheckingDaily:
    """A daily row asking "has a week passed?" is a second clock, not a weekly cadence."""

    def test_the_row_carries_the_weekly_interval(self) -> None:
        assert _row("eval_local")["delay_seconds"] == 604800

    def test_the_inner_weekly_gate_is_retired(self) -> None:
        assert "eval_local_cadence_hours" not in ALL_KNOWN_CONFIG_SETTINGS
        assert classify("eval_local_cadence_hours").classes == {SettingClass.RETIRED}


class TestTheDailyTriageRunsDailyRatherThanCheckingHourly:
    """The same shape one order of magnitude down: an hourly row gated on a daily quantity."""

    def test_the_row_carries_the_daily_interval(self) -> None:
        assert _row("triage_assessor")["delay_seconds"] == 86400

    def test_the_inner_daily_gate_is_retired(self) -> None:
        assert "triage_assessor_cadence_hours" not in ALL_KNOWN_CONFIG_SETTINGS
        assert classify("triage_assessor_cadence_hours").classes == {SettingClass.RETIRED}


class TestTheDailyBacklogSweepRunsDailyRatherThanCheckingDaily:
    """A daily row asking "has a day passed?" is the same shape at the same magnitude."""

    def test_the_row_carries_the_daily_interval(self) -> None:
        assert _row("backlog_sweep")["delay_seconds"] == 86400

    def test_the_inner_daily_gate_is_retired(self) -> None:
        assert "backlog_sweep_cadence_hours" not in ALL_KNOWN_CONFIG_SETTINGS
        assert classify("backlog_sweep_cadence_hours").classes == {SettingClass.RETIRED}


class TestTheDailyDogfoodSmokeRunsDailyRatherThanCheckingHourly:
    """An hourly-floored row gated on a daily quantity — the triage_assessor shape again."""

    def test_the_row_carries_the_daily_interval(self) -> None:
        assert _row("dogfood")["delay_seconds"] == 86400

    def test_the_inner_daily_gate_is_retired(self) -> None:
        assert "dogfood_smoke_cadence_hours" not in ALL_KNOWN_CONFIG_SETTINGS
        assert classify("dogfood_smoke_cadence_hours").classes == {SettingClass.RETIRED}


class TestTheHourlySelfUpdateRunsHourlyRatherThanCheckingHourly:
    """The same shape on the housekeeping row: an hourly row gated on an hourly marker."""

    def test_the_row_carries_the_hourly_interval(self) -> None:
        assert _row("housekeeping")["delay_seconds"] == 3600

    def test_the_inner_hourly_gate_is_retired(self) -> None:
        assert "self_update_cadence_hours" not in ALL_KNOWN_CONFIG_SETTINGS
        assert classify("self_update_cadence_hours").classes == {SettingClass.RETIRED}


class TestADifferentQuantitySurvives:
    def test_the_weekly_architectural_review_gate_is_not_folded_into_its_daily_row(self) -> None:
        """The control: a fold that took this too would be deleting a real second quantity."""
        assert _row("arch_review")["daily_at"]
        assert ALL_KNOWN_CONFIG_SETTINGS["architectural_review_cadence_hours"]
