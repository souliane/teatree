"""Coverage floor inspection — overall ``fail_under`` plus per-module floors.

Reads ``[tool.coverage.report] fail_under`` and ``[tool.teatree.coverage]
per_module_floors`` from ``pyproject.toml`` and combines them with the most
recent ``.coverage`` data file to produce a :class:`CoverageReport` the CLI
and tests can act on.

The overall floor is the same one pytest-cov enforces during the test run.
Per-module floors are checked separately because they target newly-added
modules where a 93% project-wide gate isn't tight enough — a small new file
can drop to 30% and still leave the project >= 93%.
"""

import io
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import coverage as _coverage


class ModuleCoverageDict(TypedDict):
    path: str
    floor: int
    percent: float | None


class CoverageReportDict(TypedDict):
    overall_percent: float | None
    overall_floor: int
    modules: list[ModuleCoverageDict]
    passes: bool


@dataclass(frozen=True)
class ModuleCoverage:
    path: str
    floor: int
    percent: float | None

    def passes(self) -> bool:
        return self.percent is not None and self.percent >= self.floor

    def to_dict(self) -> ModuleCoverageDict:
        return {"path": self.path, "floor": self.floor, "percent": self.percent}


@dataclass(frozen=True)
class CoverageReport:
    overall_percent: float | None
    overall_floor: int
    module_results: list[ModuleCoverage] = field(default_factory=list)

    def passes(self) -> bool:
        if self.overall_percent is None:
            return False
        if round(self.overall_percent) < self.overall_floor:
            return False
        return all(m.passes() for m in self.module_results)

    def failed_modules(self) -> list[ModuleCoverage]:
        return [m for m in self.module_results if not m.passes()]

    def to_dict(self) -> CoverageReportDict:
        return {
            "overall_percent": self.overall_percent,
            "overall_floor": self.overall_floor,
            "modules": [m.to_dict() for m in self.module_results],
            "passes": self.passes(),
        }


def _load_pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_overall_floor(pyproject_path: Path) -> int:
    data = _load_pyproject(pyproject_path)
    return int(data["tool"]["coverage"]["report"]["fail_under"])


def load_per_module_floors(pyproject_path: Path) -> dict[str, int]:
    data = _load_pyproject(pyproject_path)
    teatree_cov = data.get("tool", {}).get("teatree", {}).get("coverage", {})
    entries = teatree_cov.get("per_module_floors", [])
    return {entry["path"]: int(entry["floor"]) for entry in entries}


def _module_percent(cov: "_coverage.Coverage", file_path: str) -> float | None:
    try:
        _, executable, _, missing, _ = cov.analysis2(file_path)
    except Exception:  # noqa: BLE001 — coverage raises various NoSource/CoverageException types
        return None
    total = len(executable)
    if total == 0:
        return None
    return 100.0 * (total - len(missing)) / total


def measure_coverage(
    pyproject_path: Path,
    coverage_data_file: Path,
    per_module_floors: dict[str, int] | None = None,
) -> CoverageReport:
    overall_floor = load_overall_floor(pyproject_path)
    floors = per_module_floors if per_module_floors is not None else load_per_module_floors(pyproject_path)

    if not coverage_data_file.exists():
        return CoverageReport(overall_percent=None, overall_floor=overall_floor, module_results=[])

    import coverage  # noqa: PLC0415 — heavy import, only needed when .coverage exists

    cov = coverage.Coverage(data_file=str(coverage_data_file))
    cov.load()

    measured = {Path(f).resolve(): f for f in cov.get_data().measured_files()}
    module_results: list[ModuleCoverage] = []
    for declared_path, floor in floors.items():
        resolved = Path(declared_path).resolve()
        actual = measured.get(resolved)
        percent = _module_percent(cov, actual) if actual else None
        module_results.append(ModuleCoverage(path=declared_path, floor=floor, percent=percent))

    overall_percent: float | None
    try:
        overall_percent = cov.report(file=io.StringIO(), output_format="text")
    except Exception:  # noqa: BLE001 — an unparsable coverage report degrades to unknown percent
        overall_percent = None

    return CoverageReport(
        overall_percent=overall_percent,
        overall_floor=overall_floor,
        module_results=module_results,
    )
