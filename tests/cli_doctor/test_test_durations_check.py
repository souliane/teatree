"""``check_test_durations_coverage`` — the doctor's blind-shard-split gate (#4048)."""

from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.app import run_doctor_checks
from teatree.cli.doctor.checks_test_durations import check_test_durations_coverage
from teatree.quality.durations_coverage import DurationsCoverage, DurationsUnreadableError


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

    def test_blind_split_fails_and_names_the_refresh_branch(self, capsys, tmp_path: Path) -> None:
        with _repo_found(tmp_path), _measured(DurationsCoverage(covered_files=246, test_files=2207, orphan_keys=73)):
            assert check_test_durations_coverage() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "11.1%" in out
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
