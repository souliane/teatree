"""The one duration-spec parser shared by every ``--for``/``--since`` flag."""

import datetime as dt

import pytest

from teatree.core.modelkit.durations import format_age, format_window, parse_duration


class TestParseDuration:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("45s", dt.timedelta(seconds=45)),
            ("30m", dt.timedelta(minutes=30)),
            ("2h", dt.timedelta(hours=2)),
            ("1d", dt.timedelta(days=1)),
            ("0h", dt.timedelta(0)),
            ("  3d  ", dt.timedelta(days=3)),
        ],
    )
    def test_parses_every_supported_unit(self, raw: str, expected: dt.timedelta) -> None:
        assert parse_duration(raw, flag="--since") == expected

    @pytest.mark.parametrize("raw", ["", "24", "1w", "h", "-2h", "2.5h", "2 h", "2hh"])
    def test_rejects_an_unparseable_spec(self, raw: str) -> None:
        with pytest.raises(ValueError, match="use forms like 2h, 30m, 1d"):
            parse_duration(raw, flag="--since")

    def test_the_refusal_names_the_flag_that_was_wrong(self) -> None:
        with pytest.raises(ValueError, match=r"invalid --for duration 'nope'"):
            parse_duration("nope", flag="--for")


class TestFormatWindow:
    @pytest.mark.parametrize(
        ("window", "expected"),
        [
            (dt.timedelta(hours=24), "1d"),
            (dt.timedelta(days=3), "3d"),
            (dt.timedelta(hours=5), "5h"),
            (dt.timedelta(minutes=90), "90m"),
            (dt.timedelta(seconds=45), "45s"),
            (dt.timedelta(0), "0s"),
        ],
    )
    def test_renders_the_largest_evenly_dividing_unit(self, window: dt.timedelta, expected: str) -> None:
        assert format_window(window) == expected


class TestFormatAge:
    def test_an_undated_stamp_says_so_rather_than_guessing(self) -> None:
        assert format_age(None, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) == "undated"

    @pytest.mark.parametrize(
        ("ago", "expected"),
        [
            (dt.timedelta(days=2, hours=3), "2d ago"),
            (dt.timedelta(hours=3), "3h ago"),
            (dt.timedelta(minutes=90), "1h ago"),
            (dt.timedelta(seconds=20), "20s ago"),
        ],
    )
    def test_reports_one_coarse_unit(self, ago: dt.timedelta, expected: str) -> None:
        now = dt.datetime(2026, 1, 10, tzinfo=dt.UTC)

        assert format_age(now - ago, now=now) == expected

    def test_a_future_stamp_clamps_rather_than_reporting_a_negative_age(self) -> None:
        now = dt.datetime(2026, 1, 10, tzinfo=dt.UTC)

        assert format_age(now + dt.timedelta(hours=1), now=now) == "0s ago"
