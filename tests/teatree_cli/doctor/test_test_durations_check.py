"""The doctor's two readings of ``dev/.test_durations`` — blind split, and ceiling pressure (#4048)."""

from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.app import run_doctor_checks
from teatree.cli.doctor.checks_test_durations import check_test_durations_coverage, check_test_timeout_headroom
from teatree.quality.durations_coverage import DurationsCoverage
from teatree.quality.durations_file import DurationsUnreadableError
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


def _headroom(report: HeadroomReport | None):
    return patch(
        "teatree.quality.timeout_headroom.measure_timeout_headroom",
        return_value=report,
    )


def _report(*pressured: CeilingPressure, unresolved: int = 0) -> HeadroomReport:
    return HeadroomReport(pressured=pressured, judged=100, unresolved_ceilings=unresolved)


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

    def test_a_recorded_over_run_fails_and_names_the_test(self, capsys, tmp_path: Path) -> None:
        over = CeilingPressure(node_id="tests/test_b.py::test_slow", seconds=180.2, ceiling=180.0)
        with _repo_found(tmp_path), _headroom(_report(over)):
            assert check_test_timeout_headroom() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "tests/test_b.py::test_slow" in out

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
