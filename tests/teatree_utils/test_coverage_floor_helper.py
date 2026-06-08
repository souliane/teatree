"""Tests for ``teatree.utils.coverage_floor`` — coverage floor inspection helper.

The helper powers ``t3 ci coverage`` and provides programmatic access to the
overall ``fail_under`` floor and the per-module floors declared under
``[tool.teatree.coverage]``. Devs use it before pushing to verify they
haven't dropped a module under its target threshold.
"""

import json
from pathlib import Path
from textwrap import dedent

import pytest

from teatree.utils.coverage_floor import (
    CoverageReport,
    ModuleCoverage,
    load_overall_floor,
    load_per_module_floors,
    measure_coverage,
)


@pytest.fixture
def fake_pyproject(tmp_path: Path) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        dedent(
            """\
            [tool.coverage.report]
            fail_under = 93

            [tool.coverage.run]
            source = ["src/teatree"]
            omit = ["src/teatree/core/migrations/*.py"]

            [tool.teatree.coverage]
            per_module_floors = [
                { path = "src/teatree/loop/persistence.py", floor = 80 },
                { path = "src/teatree/loop/rendering.py", floor = 80 },
                { path = "src/teatree/loop/dispatch.py", floor = 80 },
            ]
            """,
        ),
        encoding="utf-8",
    )
    return pyproject


class TestLoadOverallFloor:
    def test_returns_fail_under(self, fake_pyproject: Path) -> None:
        assert load_overall_floor(fake_pyproject) == 93

    def test_raises_when_missing(self, tmp_path: Path) -> None:
        empty = tmp_path / "pyproject.toml"
        empty.write_text("[project]\nname = 'x'\n", encoding="utf-8")
        with pytest.raises(KeyError):
            load_overall_floor(empty)


class TestLoadPerModuleFloors:
    def test_returns_mapping(self, fake_pyproject: Path) -> None:
        floors = load_per_module_floors(fake_pyproject)
        assert floors == {
            "src/teatree/loop/persistence.py": 80,
            "src/teatree/loop/rendering.py": 80,
            "src/teatree/loop/dispatch.py": 80,
        }

    def test_returns_empty_when_absent(self, tmp_path: Path) -> None:
        empty = tmp_path / "pyproject.toml"
        empty.write_text("[project]\nname = 'x'\n", encoding="utf-8")
        assert load_per_module_floors(empty) == {}


class TestMeasureCoverage:
    def test_returns_none_overall_when_no_coverage_file(self, fake_pyproject: Path, tmp_path: Path) -> None:
        report = measure_coverage(
            pyproject_path=fake_pyproject,
            coverage_data_file=tmp_path / ".coverage-missing",
        )
        assert report.overall_percent is None
        assert report.overall_floor == 93
        assert report.module_results == []
        assert not report.passes()

    def test_returns_report_with_module_results(self, fake_pyproject: Path, tmp_path: Path) -> None:
        # Build a fake .coverage file via the coverage API.
        import coverage  # noqa: PLC0415

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        src_dir = repo_root / "src" / "teatree" / "loop"
        src_dir.mkdir(parents=True)
        (src_dir / "persistence.py").write_text(
            "def f(x):\n    if x:\n        return 1\n    return 2\n",
            encoding="utf-8",
        )

        cov_file = tmp_path / ".coverage-fake"
        cov = coverage.Coverage(data_file=str(cov_file), source=[str(src_dir)])
        cov.start()
        # Import the module to execute some lines.
        import importlib.util  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location("persistence", src_dir / "persistence.py")
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.f(1)
        cov.stop()
        cov.save()

        # Tell the helper where to look.
        per_module = {str(src_dir / "persistence.py"): 50}
        report = measure_coverage(
            pyproject_path=fake_pyproject,
            coverage_data_file=cov_file,
            per_module_floors=per_module,
        )
        assert report.overall_percent is not None
        assert report.overall_percent > 0
        assert len(report.module_results) == 1
        assert report.module_results[0].path == str(src_dir / "persistence.py")
        assert report.module_results[0].floor == 50
        assert report.module_results[0].percent is not None


class TestCoverageReportPasses:
    def test_passes_when_all_floors_met(self) -> None:
        report = CoverageReport(
            overall_percent=95.0,
            overall_floor=93,
            module_results=[
                ModuleCoverage(path="a.py", floor=80, percent=90.0),
            ],
        )
        assert report.passes()
        assert report.failed_modules() == []

    def test_fails_when_overall_below_floor(self) -> None:
        report = CoverageReport(
            overall_percent=80.0,
            overall_floor=93,
            module_results=[],
        )
        assert not report.passes()

    def test_fails_when_module_below_floor(self) -> None:
        report = CoverageReport(
            overall_percent=95.0,
            overall_floor=93,
            module_results=[
                ModuleCoverage(path="a.py", floor=80, percent=50.0),
                ModuleCoverage(path="b.py", floor=80, percent=90.0),
            ],
        )
        assert not report.passes()
        failed = report.failed_modules()
        assert len(failed) == 1
        assert failed[0].path == "a.py"

    def test_overall_rounded_to_int_to_match_coverage_fail_under(self) -> None:
        # ``coverage --fail-under`` rounds to int precision by default — 92.7
        # is reported as "93%" and pytest-cov passes. ``passes()`` must agree.
        report = CoverageReport(overall_percent=92.7, overall_floor=93, module_results=[])
        assert report.passes()
        report_below = CoverageReport(overall_percent=92.4, overall_floor=93, module_results=[])
        assert not report_below.passes()

    def test_fails_when_overall_unmeasured(self) -> None:
        report = CoverageReport(overall_percent=None, overall_floor=93, module_results=[])
        assert not report.passes()

    def test_module_unmeasured_treated_as_failure(self) -> None:
        report = CoverageReport(
            overall_percent=95.0,
            overall_floor=93,
            module_results=[
                ModuleCoverage(path="a.py", floor=80, percent=None),
            ],
        )
        assert not report.passes()
        assert report.failed_modules()[0].path == "a.py"


class TestModuleCoverageJson:
    def test_to_dict(self) -> None:
        mc = ModuleCoverage(path="a.py", floor=80, percent=90.0)
        assert mc.to_dict() == {"path": "a.py", "floor": 80, "percent": 90.0}

    def test_report_to_dict(self) -> None:
        report = CoverageReport(
            overall_percent=95.0,
            overall_floor=93,
            module_results=[ModuleCoverage(path="a.py", floor=80, percent=90.0)],
        )
        data = report.to_dict()
        # Must be JSON-serialisable.
        json.dumps(data)
        assert data["overall_percent"] == pytest.approx(95.0)
        assert data["overall_floor"] == 93
        assert data["passes"] is True
