"""The weekly-eval gate fires only on the first MR opened each ISO week."""

import datetime as dt
import importlib.util
import json
from operator import itemgetter
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
select_gate_records = _MOD.select_gate_records
week_has_no_pr = _MOD.week_has_no_pr
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


class TestSelectGateRecords:
    def test_keeps_current_week_record_buried_past_the_oldest_hundred(self) -> None:
        old = [
            {"number": i, "created_at": f"2025-01-{(i % 27) + 1:02d}T09:00:00Z"}
            for i in range(1, 1700)  # ~1700 records, none in week 23 of 2026
        ]
        current_week = {"number": 1701, "created_at": "2026-06-01T09:00:00Z"}  # Monday, week 23
        corpus = [*old, current_week]

        selected = select_gate_records(corpus, now=NOW)

        assert any(rec.get("number") == 1701 for rec in selected)

    def test_drops_the_oldest_history_beyond_the_window(self) -> None:
        corpus = [{"number": i, "created_at": f"2025-03-{(i % 27) + 1:02d}T09:00:00Z"} for i in range(1, 500)]

        selected = select_gate_records(corpus, now=NOW, per_page=100)

        assert len(selected) == 100

    def test_always_includes_the_full_current_week_even_past_the_window(self) -> None:
        history = [{"number": i, "created_at": f"2025-03-{(i % 27) + 1:02d}T09:00:00Z"} for i in range(1, 300)]
        this_week = [{"number": 900 + i, "created_at": "2026-06-01T09:00:00Z"} for i in range(150)]
        corpus = [*this_week, *history]

        selected = select_gate_records(corpus, now=NOW, per_page=100)

        selected_numbers = {rec["number"] for rec in selected}
        assert all((900 + i) in selected_numbers for i in range(150))

    def test_gate_runs_on_current_week_pr_from_a_full_history(self) -> None:
        old = [{"number": i, "created_at": f"2025-03-{(i % 27) + 1:02d}T09:00:00Z"} for i in range(1, 1700)]
        current_week = {"number": 1701, "created_at": "2026-06-02T09:00:00Z"}  # Tuesday, week 23
        corpus = [*old, current_week]

        selected = select_gate_records(corpus, now=NOW)

        assert is_first_mr_of_week(selected, current_iid=1701, now=NOW) is True

    def test_skips_entries_without_a_usable_created_at(self) -> None:
        corpus = [
            {"number": 1},  # no created_at
            {"number": 2, "created_at": ""},  # falsy created_at
            {"number": 3, "created_at": "not-a-date"},  # unparsable
            {"number": 4, "created_at": "2026-06-01T09:00:00Z"},  # week 23
        ]

        selected = select_gate_records(corpus, now=NOW)

        assert [rec["number"] for rec in selected] == [4]

    def test_oldest_first_page_is_inert_but_selection_fixes_it(self) -> None:
        old = [{"number": i, "created_at": f"2025-01-{(i % 27) + 1:02d}T09:00:00Z"} for i in range(1, 1700)]
        current_week = {"number": 1701, "created_at": "2026-06-01T09:00:00Z"}  # Monday, week 23
        corpus = [*old, current_week]

        oldest_first_page = sorted(corpus, key=itemgetter("created_at"))[:100]
        assert is_first_mr_of_week(oldest_first_page, current_iid=1701, now=NOW) is False

        selected = select_gate_records(corpus, now=NOW)
        assert is_first_mr_of_week(selected, current_iid=1701, now=NOW) is True


class TestWeekHasNoPr:
    """The scheduled (cron) eval path runs only in a PR-less ISO week.

    The first-PR path already runs the eval for any week that opened a PR,
    so the shared marker between the two paths is the week's PR list: a
    week with at least one PR is covered by the first-PR path → the cron
    must skip; a week with NO PR is uncovered → the cron runs.
    """

    def test_true_when_no_pr_opened_this_week(self) -> None:
        prs = [{"number": 9, "created_at": "2026-05-28T09:00:00Z"}]  # week 22, prior week
        assert week_has_no_pr(prs, now=NOW) is True

    def test_false_when_a_pr_opened_this_week(self) -> None:
        prs = [{"number": 10, "created_at": "2026-06-01T09:00:00Z"}]  # Monday, week 23
        assert week_has_no_pr(prs, now=NOW) is False

    def test_true_for_empty_list(self) -> None:
        assert week_has_no_pr([], now=NOW) is True

    def test_ignores_malformed_and_out_of_week_records(self) -> None:
        prs = [
            {"number": None, "created_at": "2026-06-01T09:00:00Z"},
            {"number": 11, "created_at": "not-a-date"},
            {"number": 12, "created_at": "2026-05-20T09:00:00Z"},  # week 21
        ]
        assert week_has_no_pr(prs, now=NOW) is True

    def test_complements_first_pr_path_for_a_pr_week(self) -> None:
        """When a PR exists this week, the first-PR gate covers it and cron skips."""
        prs = [{"number": 10, "created_at": "2026-06-02T09:00:00Z"}]  # Tuesday, week 23
        assert is_first_mr_of_week(prs, current_iid=10, now=NOW) is True
        assert week_has_no_pr(prs, now=NOW) is False


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

    def test_no_pr_week_mode_exit_zero_when_no_pr_this_week(self, tmp_path: Path) -> None:
        """The cron path: ``--mode no-pr-week`` runs the eval in a PR-less week."""
        mrs_file = tmp_path / "prs.json"
        mrs_file.write_text(json.dumps([{"number": 9, "created_at": "2026-05-28T09:00:00Z"}]), encoding="utf-8")
        code = main(["--mrs-file", str(mrs_file), "--mode", "no-pr-week", "--now", "2026-06-03T12:00:00Z"])
        assert code == 0

    def test_no_pr_week_mode_exit_skip_when_a_pr_opened_this_week(self, tmp_path: Path) -> None:
        """The cron path skips when the first-PR path already covered this week."""
        mrs_file = tmp_path / "prs.json"
        mrs_file.write_text(json.dumps([{"number": 10, "created_at": "2026-06-01T09:00:00Z"}]), encoding="utf-8")
        argv = ["--mrs-file", str(mrs_file), "--mode", "no-pr-week", "--now", "2026-06-03T12:00:00Z"]
        code = main([*argv, "--skip-code", "1"])
        assert code == 1
