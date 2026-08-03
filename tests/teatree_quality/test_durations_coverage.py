"""The durations-coverage measurement behind the doctor's blind-split gate (#4048)."""

import json
from pathlib import Path

import pytest

from teatree.quality.durations_coverage import MIN_FILE_COVERAGE, DurationsUnreadableError, measure_durations_coverage


def _repo(tmp_path: Path, *, test_files: list[str], durations: dict[str, float] | None) -> Path:
    for rel in test_files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_x() -> None:\n    pass\n", encoding="utf-8")
    if durations is not None:
        (tmp_path / "dev").mkdir(exist_ok=True)
        (tmp_path / "dev" / ".test_durations").write_text(json.dumps(durations), encoding="utf-8")
    return tmp_path


class TestMeasureDurationsCoverage:
    def test_not_a_checkout_is_unanswerable_not_a_verdict(self, tmp_path: Path) -> None:
        assert measure_durations_coverage(tmp_path) is None

    def test_every_test_file_recorded_is_full_coverage(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            test_files=["tests/a/test_one.py", "tests/b/test_two.py"],
            durations={"tests/a/test_one.py::test_x": 1.0, "tests/b/test_two.py::test_x": 2.0},
        )
        coverage = measure_durations_coverage(repo)
        assert coverage is not None
        assert (coverage.covered_files, coverage.test_files) == (2, 2)
        assert coverage.ratio == pytest.approx(1.0)
        assert coverage.is_healthy

    def test_unrecorded_files_drag_the_ratio_down(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            test_files=[f"tests/test_{n}.py" for n in range(10)],
            durations={"tests/test_0.py::test_x": 1.0},
        )
        coverage = measure_durations_coverage(repo)
        assert coverage is not None
        assert coverage.ratio == pytest.approx(0.1)
        assert coverage.ratio < MIN_FILE_COVERAGE

    def test_absent_durations_file_reads_as_zero_not_as_healthy(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, test_files=["tests/test_one.py"], durations=None)
        coverage = measure_durations_coverage(repo)
        assert coverage is not None
        assert (coverage.covered_files, coverage.ratio) == (0, 0.0)

    def test_unreadable_durations_file_raises_never_degrades_to_empty(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, test_files=["tests/test_one.py"], durations={"tests/test_one.py::test_x": 1.0})
        (repo / "dev" / ".test_durations").write_text("{not json", encoding="utf-8")
        with pytest.raises(DurationsUnreadableError):
            measure_durations_coverage(repo)

    def test_keys_whose_file_is_gone_are_counted_as_orphans(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            test_files=["tests/test_one.py"],
            durations={"tests/test_one.py::test_x": 1.0, "tests/test_deleted.py::test_x": 3.0},
        )
        coverage = measure_durations_coverage(repo)
        assert coverage is not None
        assert coverage.orphan_keys == 1

    def test_src_doctest_keys_never_inflate_the_test_file_ratio(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            test_files=["tests/test_one.py", "tests/test_two.py"],
            durations={"src/teatree/thing.py::teatree.thing.f": 0.1, "tests/test_one.py::test_x": 1.0},
        )
        coverage = measure_durations_coverage(repo)
        assert coverage is not None
        assert (coverage.covered_files, coverage.test_files) == (1, 2)


class TestAgainstThisRepo:
    """The guard must be non-vacuous on the real tree it ships in."""

    def test_measures_the_live_checkout_without_raising(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        coverage = measure_durations_coverage(repo)
        assert coverage is not None
        assert coverage.test_files > 0
