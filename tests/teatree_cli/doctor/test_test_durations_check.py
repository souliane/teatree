"""The doctor's readings of ``dev/.test_durations`` — blind split, ceiling pressure, refresh age (#4048, #4130)."""

import datetime as dt
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.app import run_doctor_checks
from teatree.cli.doctor.checks_test_durations import (
    check_test_durations_coverage,
    check_test_durations_freshness,
    check_test_timeout_headroom,
)
from teatree.quality.durations_coverage import DurationsCoverage
from teatree.quality.durations_file import DurationsUnreadableError
from teatree.quality.durations_freshness import MAX_REFRESH_AGE, DurationsFreshness, DurationsHistoryUnreadableError
from teatree.quality.timeout_headroom import CeilingPressure, HeadroomReport


def _measured(coverage: DurationsCoverage | None):
    return patch(
        "teatree.quality.durations_coverage.measure_durations_coverage",
        return_value=coverage,
    )


def _repo_found(tmp_path: Path):
    return patch("teatree.cli.doctor.service.DoctorService.find_teatree_repo", return_value=tmp_path)


class TestTestDurationsDoctorCheck:
    def test_healthy_coverage_is_ok(self, capsys, tmp_path: Path) -> None:
        with _repo_found(tmp_path), _measured(DurationsCoverage(covered_files=99, test_files=100, orphan_keys=0)):
            assert check_test_durations_coverage() is True
        assert "OK" in capsys.readouterr().out

    def test_blind_split_warns_and_names_the_refresh_branch(self, capsys, tmp_path: Path) -> None:
        """Reported at every session start, but never a FAIL — see the check's own docstring.

        A FAIL is consumed by ``deploy/watchdog.sh``, which DMs the owner every
        non-deploy-sensitive FAIL line on a 300s cadence, re-keyed daily. Staleness
        stands until a refresh PR is merged, so a FAIL here is a standing nightly page
        for something no actor caused. The numbers and the remedy still print.
        """
        with _repo_found(tmp_path), _measured(DurationsCoverage(covered_files=246, test_files=2207, orphan_keys=73)):
            assert check_test_durations_coverage() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "FAIL" not in out
        assert "11.1%" in out
        assert "73 recorded key(s) name a deleted file" in out
        assert "ci/test-durations-refresh" in out

    def test_no_checkout_is_silent_never_a_verdict(self, capsys, tmp_path: Path) -> None:
        with _repo_found(tmp_path), _measured(None):
            assert check_test_durations_coverage() is True
        assert capsys.readouterr().out == ""

    def test_no_repo_resolved_is_silent(self, capsys) -> None:
        with patch("teatree.cli.doctor.service.DoctorService.find_teatree_repo", return_value=None):
            assert check_test_durations_coverage() is True
        assert capsys.readouterr().out == ""

    def test_the_aggregate_actually_calls_it(self) -> None:
        """A check nothing invokes is a check that reports nothing (#4048)."""
        assert "check_test_durations_coverage" in run_doctor_checks.__code__.co_names

    def test_a_non_utf8_durations_file_is_reported_not_a_crash(self, capsys, tmp_path: Path) -> None:
        """The real file, not a stubbed measurement: an unwrapped read error took down the whole run."""
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntimeout = 60\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_a() -> None:\n    pass\n", encoding="utf-8")
        (tmp_path / "dev").mkdir()
        (tmp_path / "dev" / ".test_durations").write_bytes(b'\xff\xfe{"a": 1}')

        with _repo_found(tmp_path):
            assert check_test_durations_coverage() is False
            assert check_test_timeout_headroom() is True

        out = capsys.readouterr().out
        assert "FAIL  Test-shard durations:" in out
        assert "could not be read as durations JSON" in out

    def test_unreadable_durations_file_fails_loud_never_silent(self, capsys, tmp_path: Path) -> None:
        with (
            _repo_found(tmp_path),
            patch(
                "teatree.quality.durations_coverage.measure_durations_coverage",
                side_effect=DurationsUnreadableError("boom"),
            ),
        ):
            assert check_test_durations_coverage() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "boom" in out


def _headroom(report: HeadroomReport | None):
    return patch(
        "teatree.quality.timeout_headroom.measure_timeout_headroom",
        return_value=report,
    )


def _report(
    *pressured: CeilingPressure, unresolved: int = 0, shielded: tuple[CeilingPressure, ...] = ()
) -> HeadroomReport:
    return HeadroomReport(pressured=pressured, shielded=shielded, judged=100, unresolved_ceilings=unresolved)


class TestTimeoutHeadroomDoctorCheck:
    def test_room_to_spare_is_silent(self, capsys, tmp_path: Path) -> None:
        with _repo_found(tmp_path), _headroom(_report()):
            assert check_test_timeout_headroom() is True
        assert capsys.readouterr().out == ""

    def test_a_squeeze_warns_and_names_the_test_without_failing(self, capsys, tmp_path: Path) -> None:
        squeezed = CeilingPressure(node_id="tests/test_a.py::test_slow", seconds=56.85, ceiling=60.0)
        with _repo_found(tmp_path), _headroom(_report(squeezed)):
            assert check_test_timeout_headroom() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "FAIL" not in out
        assert "tests/test_a.py::test_slow" in out

    def test_a_shielded_test_is_counted_beside_the_squeeze_it_is_not(self, capsys, tmp_path: Path) -> None:
        squeezed = CeilingPressure(node_id="tests/test_a.py::test_slow", seconds=56.85, ceiling=60.0)
        covered = CeilingPressure(node_id="tests/test_b.py::test_slow", seconds=100.0, ceiling=240.0)
        with _repo_found(tmp_path), _headroom(_report(squeezed, shielded=(covered,))):
            assert check_test_timeout_headroom() is True
        out = capsys.readouterr().out
        assert "1 more run past" in out
        assert "state their own higher ceiling" in out

    def test_a_healthy_run_stays_silent_even_with_shielded_tests(self, capsys, tmp_path: Path) -> None:
        """The count is context for a report already being printed, never a reason to start one."""
        covered = CeilingPressure(node_id="tests/test_b.py::test_slow", seconds=100.0, ceiling=240.0)
        with _repo_found(tmp_path), _headroom(_report(shielded=(covered,))):
            assert check_test_timeout_headroom() is True
        assert capsys.readouterr().out == ""

    def test_a_recorded_over_run_fails_and_names_the_test(self, capsys, tmp_path: Path) -> None:
        over = CeilingPressure(node_id="tests/test_b.py::test_slow", seconds=180.2, ceiling=180.0)
        with _repo_found(tmp_path), _headroom(_report(over)):
            assert check_test_timeout_headroom() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "tests/test_b.py::test_slow" in out

    def test_the_over_run_remedy_says_which_half_clears_it_now(self, capsys, tmp_path: Path) -> None:
        """The two remedies do not clear the FAIL alike, and the message must not imply they do (#4130).

        The ceiling is read live from source, so raising the marker clears the next run.
        The recorded seconds come from the committed artifact, so making the test faster
        leaves the FAIL standing until the next refresh lands — an operator told only
        "make each faster" reasonably expects their fix to clear it, and it does not.
        """
        over = CeilingPressure(node_id="tests/test_b.py::test_slow", seconds=180.2, ceiling=180.0)
        with _repo_found(tmp_path), _headroom(_report(over)):
            assert check_test_timeout_headroom() is False
        out = capsys.readouterr().out
        assert "clears this now" in out
        assert "until the next durations refresh lands" in out

    def test_a_long_list_is_truncated_with_an_honest_trailer(self, capsys, tmp_path: Path) -> None:
        squeezed = [
            CeilingPressure(node_id=f"tests/test_{n}.py::test_slow", seconds=50.0, ceiling=60.0) for n in range(9)
        ]
        with _repo_found(tmp_path), _headroom(_report(*squeezed)):
            assert check_test_timeout_headroom() is True
        assert "… and 4 more" in capsys.readouterr().out

    def test_unjudged_files_are_declared_so_the_list_is_not_read_as_complete(self, capsys, tmp_path: Path) -> None:
        squeezed = CeilingPressure(node_id="tests/test_a.py::test_slow", seconds=56.0, ceiling=60.0)
        with _repo_found(tmp_path), _headroom(_report(squeezed, unresolved=3)):
            assert check_test_timeout_headroom() is True
        assert "3 file(s) name their ceiling" in capsys.readouterr().out

    def test_no_ceiling_to_judge_against_is_silent_never_a_verdict(self, capsys, tmp_path: Path) -> None:
        with _repo_found(tmp_path), _headroom(None):
            assert check_test_timeout_headroom() is True
        assert capsys.readouterr().out == ""

    def test_no_repo_resolved_is_silent(self, capsys) -> None:
        with patch("teatree.cli.doctor.service.DoctorService.find_teatree_repo", return_value=None):
            assert check_test_timeout_headroom() is True
        assert capsys.readouterr().out == ""

    def test_an_unreadable_file_defers_to_the_coverage_check_rather_than_double_failing(
        self, capsys, tmp_path: Path
    ) -> None:
        with (
            _repo_found(tmp_path),
            patch(
                "teatree.quality.timeout_headroom.measure_timeout_headroom",
                side_effect=DurationsUnreadableError("boom"),
            ),
        ):
            assert check_test_timeout_headroom() is True
        assert capsys.readouterr().out == ""

    def test_the_aggregate_actually_calls_it(self) -> None:
        """A check nothing invokes is a check that reports nothing (#4048)."""
        assert "check_test_timeout_headroom" in run_doctor_checks.__code__.co_names


def _freshness(measurement: DurationsFreshness | None):
    return patch(
        "teatree.quality.durations_freshness.measure_durations_freshness",
        return_value=measurement,
    )


def _aged(age: dt.timedelta) -> DurationsFreshness:
    return DurationsFreshness(last_refreshed_at=dt.datetime(2026, 8, 6, tzinfo=dt.UTC) - age, age=age)


class TestDurationsFreshnessDoctorCheck:
    def test_a_stale_artifact_fails_and_names_the_refresh_workflow(self, capsys, tmp_path: Path) -> None:
        """The alarm the coverage WARN cannot raise (#4130).

        A fully-stale but parseable file trips neither retained FAIL — the unreadable-file
        one needs corruption, and the over-run one needs a recorded over-run. So the state
        this check exists for produced zero FAILs, and nothing paged when the refresh
        pipeline stopped.
        """
        with _repo_found(tmp_path), _freshness(_aged(MAX_REFRESH_AGE + dt.timedelta(days=6))):
            assert check_test_durations_freshness() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "ci/test-durations-refresh" in out
        assert f"{(MAX_REFRESH_AGE + dt.timedelta(days=6)).days} days" in out

    def test_a_low_coverage_file_being_refreshed_normally_never_pages(self, capsys, tmp_path: Path) -> None:
        """The pair the ticket's acceptance names: shortfall stays a WARN, freshness stays quiet.

        This is the state #4113 deliberately declined to page on — coverage climbing back
        while the refresh catches up. Adding an age FAIL must not re-page it.
        """
        recent = _aged(dt.timedelta(days=1))
        with (
            _repo_found(tmp_path),
            _measured(DurationsCoverage(covered_files=246, test_files=2207, orphan_keys=73)),
            _freshness(recent),
        ):
            assert check_test_durations_coverage() is True
            assert check_test_durations_freshness() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "FAIL" not in out

    def test_a_fresh_artifact_reports_ok_with_its_age(self, capsys, tmp_path: Path) -> None:
        with _repo_found(tmp_path), _freshness(_aged(dt.timedelta(days=3))):
            assert check_test_durations_freshness() is True
        assert "OK" in capsys.readouterr().out

    def test_a_single_day_old_refresh_is_singular(self, capsys, tmp_path: Path) -> None:
        with _repo_found(tmp_path), _freshness(_aged(dt.timedelta(days=1))):
            assert check_test_durations_freshness() is True
        out = capsys.readouterr().out
        assert "refreshed 1 day ago" in out
        assert "1 days ago" not in out

    def test_an_unanswerable_age_is_silent_never_a_verdict(self, capsys, tmp_path: Path) -> None:
        """A shallow clone cannot see when the artifact last changed — silence beats a guess."""
        with _repo_found(tmp_path), _freshness(None):
            assert check_test_durations_freshness() is True
        assert capsys.readouterr().out == ""

    def test_no_repo_resolved_is_silent(self, capsys) -> None:
        with patch("teatree.cli.doctor.service.DoctorService.find_teatree_repo", return_value=None):
            assert check_test_durations_freshness() is True
        assert capsys.readouterr().out == ""

    def test_a_history_git_refuses_reads_as_unverified_not_as_healthy(self, capsys, tmp_path: Path) -> None:
        """A check that has quietly stopped measuring is the exact gap this one closes.

        WARN, not FAIL: an unreadable history is a fault of the venue, not of the refresh
        pipeline, so it must be visible without joining the pager.
        """
        with (
            _repo_found(tmp_path),
            patch(
                "teatree.quality.durations_freshness.measure_durations_freshness",
                side_effect=DurationsHistoryUnreadableError("dubious ownership"),
            ),
        ):
            assert check_test_durations_freshness() is True
        out = capsys.readouterr().out
        assert "UNVERIFIED" in out
        assert "dubious ownership" in out
        assert "FAIL" not in out

    def test_a_crashing_measurement_degrades_to_ok_never_aborts_the_run(self, capsys, tmp_path: Path) -> None:
        with (
            _repo_found(tmp_path),
            patch(
                "teatree.quality.durations_freshness.measure_durations_freshness",
                side_effect=OSError("git is not on PATH"),
            ),
        ):
            assert check_test_durations_freshness() is True
        assert "FAIL" not in capsys.readouterr().out

    def test_the_aggregate_actually_calls_it(self) -> None:
        """A check nothing invokes is a check that reports nothing (#4048)."""
        assert "check_test_durations_freshness" in run_doctor_checks.__code__.co_names
