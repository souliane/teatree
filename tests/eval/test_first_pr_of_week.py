"""The weekly-eval gate fires only on the first MR opened each ISO week."""

import datetime as dt
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "first_pr_of_week",
    Path(__file__).parents[2] / "scripts" / "eval" / "first_pr_of_week.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

is_first_mr_of_week = _MOD.is_first_mr_of_week
main = _MOD.main

NOW = dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.UTC)  # Wednesday, ISO week 23


class TestIsFirstMrOfWeek:
    def test_true_for_earliest_mr_in_current_week(self) -> None:
        mrs = [
            {"iid": 10, "created_at": "2026-06-01T09:00:00Z"},  # Monday, week 23
            {"iid": 11, "created_at": "2026-06-02T09:00:00Z"},  # Tuesday, week 23
        ]
        assert is_first_mr_of_week(mrs, current_iid=10, now=NOW) is True

    def test_false_for_later_mr_in_same_week(self) -> None:
        mrs = [
            {"iid": 10, "created_at": "2026-06-01T09:00:00Z"},
            {"iid": 11, "created_at": "2026-06-02T09:00:00Z"},
        ]
        assert is_first_mr_of_week(mrs, current_iid=11, now=NOW) is False

    def test_ignores_mrs_from_prior_weeks(self) -> None:
        mrs = [
            {"iid": 9, "created_at": "2026-05-28T09:00:00Z"},  # week 22
            {"iid": 12, "created_at": "2026-06-03T08:00:00Z"},  # week 23
        ]
        assert is_first_mr_of_week(mrs, current_iid=12, now=NOW) is True

    def test_false_when_no_mr_in_current_week(self) -> None:
        mrs = [{"iid": 9, "created_at": "2026-05-28T09:00:00Z"}]
        assert is_first_mr_of_week(mrs, current_iid=9, now=NOW) is False

    def test_decision_is_order_independent(self) -> None:
        forward = [
            {"iid": 10, "created_at": "2026-06-01T09:00:00Z"},
            {"iid": 11, "created_at": "2026-06-02T09:00:00Z"},
        ]
        assert is_first_mr_of_week(forward, current_iid=10, now=NOW) is True
        assert is_first_mr_of_week(list(reversed(forward)), current_iid=10, now=NOW) is True

    def test_accepts_github_number_field(self) -> None:
        mrs = [{"number": 42, "created_at": "2026-06-01T09:00:00Z"}]
        assert is_first_mr_of_week(mrs, current_iid=42, now=NOW) is True

    def test_skips_malformed_entries(self) -> None:
        mrs = [
            {"iid": None, "created_at": "2026-06-01T09:00:00Z"},
            {"iid": 11, "created_at": "not-a-date"},
            {"iid": 12, "created_at": "2026-06-01T10:00:00Z"},
        ]
        assert is_first_mr_of_week(mrs, current_iid=12, now=NOW) is True


class TestMain:
    def test_exit_zero_when_first(self, tmp_path: Path) -> None:
        mrs_file = tmp_path / "mrs.json"
        mrs_file.write_text(json.dumps([{"iid": 10, "created_at": "2026-06-01T09:00:00Z"}]), encoding="utf-8")
        code = main(["--mrs-file", str(mrs_file), "--current-iid", "10", "--now", "2026-06-03T12:00:00Z"])
        assert code == 0

    def test_exit_skip_code_when_not_first(self, tmp_path: Path) -> None:
        mrs_file = tmp_path / "mrs.json"
        mrs_file.write_text(
            json.dumps(
                [
                    {"iid": 10, "created_at": "2026-06-01T09:00:00Z"},
                    {"iid": 11, "created_at": "2026-06-02T09:00:00Z"},
                ]
            ),
            encoding="utf-8",
        )
        argv = ["--mrs-file", str(mrs_file), "--current-iid", "11", "--now", "2026-06-03T12:00:00Z", "--skip-code", "1"]
        code = main(argv)
        assert code == 1

    def test_exit_two_on_non_list(self, tmp_path: Path) -> None:
        mrs_file = tmp_path / "mrs.json"
        mrs_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        code = main(["--mrs-file", str(mrs_file), "--current-iid", "1"])
        assert code == 2
