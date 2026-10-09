"""Per-file CPU and wall seconds of a pytest run, recorded by a plugin inside the child.

The plugin (``-p tests._file_cost``) brackets each item's setup, call and teardown. A
subprocess's CPU counts once it is reaped, import and collection time fall outside the
item windows, and the child runs serial (``-n 0``) because xdist workers' CPU is never
seen by the recorder.
"""

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

PLUGIN_MODULE = "tests._file_cost"
_REPORT_OPTION = "--file-cost-report"
_IMPORT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class FileCost:
    cpu_s: float
    wall_s: float


@dataclass(frozen=True, slots=True)
class MeasuredRun:
    returncode: int
    stdout: str
    stderr: str
    costs: Mapping[str, FileCost]


def cpu_seconds() -> float:
    """CPU of this process plus its already-reaped children; a child still running is not counted."""
    own = resource.getrusage(resource.RUSAGE_SELF)
    reaped = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + reaped.ru_utime + reaped.ru_stime


def over_budget(costs: Mapping[str, FileCost], budget_s: float) -> dict[str, FileCost]:
    return {file: cost for file, cost in costs.items() if cost.cpu_s > budget_s}


def unmeasured(declared: Iterable[str], costs: Mapping[str, FileCost]) -> list[str]:
    return sorted(set(declared).difference(costs))


def settle(first: Mapping[str, FileCost], second: Mapping[str, FileCost], budget_s: float) -> dict[str, FileCost]:
    """Files still over budget at the lower CPU of the two runs; one missing from the second stays flagged."""
    return {file: cost for file, cost in first.items() if min(cost.cpu_s, second.get(file, cost).cpu_s) > budget_s}


def _reading(cost: FileCost) -> str:
    return f"{cost.cpu_s:.1f}s CPU ({cost.wall_s:.1f}s wall)"


def describe(costs: Mapping[str, FileCost]) -> str:
    return ", ".join(f"{file}: {_reading(cost)}" for file, cost in costs.items())


def describe_runs(first: Mapping[str, FileCost], second: Mapping[str, FileCost]) -> str:
    return "; ".join(
        f"{file}: {_reading(cost)}, re-run {_reading(second[file]) if file in second else 'not measured'}"
        for file, cost in first.items()
    )


def run_pytest_measured(pytest_args: Sequence[str], *, cwd: Path, timeout_s: float) -> MeasuredRun:
    python_path = os.pathsep.join(filter(None, [str(_IMPORT_ROOT), os.environ.get("PYTHONPATH")]))
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "file-costs.json"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                PLUGIN_MODULE,
                f"{_REPORT_OPTION}={report}",
                "-n",
                "0",
                *pytest_args,
            ],
            cwd=cwd,
            env={**os.environ, "PYTHONPATH": python_path},
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        recorded = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    return MeasuredRun(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        costs={file: FileCost(**cost) for file, cost in recorded.items()},
    )


def remeasure_offenders(
    costs: Mapping[str, FileCost], *, budget_s: float, cwd: Path, timeout_s: float
) -> Mapping[str, FileCost]:
    """One fresh run of only the files over budget; empty when none is, or when that run failed or timed out."""
    offenders = over_budget(costs, budget_s)
    if not offenders:
        return {}
    try:
        run = run_pytest_measured(list(offenders), cwd=cwd, timeout_s=timeout_s)
    except subprocess.TimeoutExpired:
        return {}
    return run.costs if run.returncode == 0 else {}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(_REPORT_OPTION, action="store", default=None, help="Write per-file CPU and wall seconds as JSON.")


def pytest_configure(config: pytest.Config) -> None:
    report = config.getoption("file_cost_report")
    if report:
        config.pluginmanager.register(_FileCostRecorder(Path(report)))


class _FileCostRecorder:
    def __init__(self, report: Path) -> None:
        self._report = report
        self._cpu: defaultdict[str, float] = defaultdict(float)
        self._wall: defaultdict[str, float] = defaultdict(float)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, object]:
        cpu_before, wall_before = cpu_seconds(), time.perf_counter()
        yield
        file = item.nodeid.split("::", 1)[0]
        self._cpu[file] += cpu_seconds() - cpu_before
        self._wall[file] += time.perf_counter() - wall_before

    def pytest_sessionfinish(self) -> None:
        costs = {file: {"cpu_s": self._cpu[file], "wall_s": self._wall[file]} for file in self._cpu}
        self._report.write_text(json.dumps(costs), encoding="utf-8")
