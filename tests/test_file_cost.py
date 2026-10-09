# test-path: cross-cutting — drives tests/_file_cost.py as a real pytest child; no src/teatree/ mirror.
"""Anti-vacuity for the CPU budget behind ``TestConformanceCoreStaysUnderBudget``.

The budget is only worth having if it is red for work and green for waiting. Each scratch
file below costs a known amount of one or the other, run through the same helpers the real
budget uses, at a scale where 0.5s plays the part of the 15s per-file limit. Files over the
limit are measured once more in a fresh child; the scratch files that spike once or stay
heavy pin what that second reading can and cannot clear.
"""

import textwrap
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._file_cost import (
    FileCost,
    MeasuredRun,
    describe,
    describe_runs,
    over_budget,
    remeasure_offenders,
    run_pytest_measured,
    settle,
    unmeasured,
)

_SCALED_BUDGET_S = 0.5

_SCRATCH_FILES = {
    "test_burns_cpu.py": """
        import time

        def test_burns_cpu():
            start = time.process_time()
            while time.process_time() - start < 0.8:
                pass
        """,
    "test_sleeps.py": """
        import time

        def test_sleeps():
            time.sleep(1.0)
        """,
    "test_burns_cpu_in_a_child.py": """
        import subprocess
        import sys

        _BURN = "import time; start = time.process_time()\\nwhile time.process_time() - start < 0.8: pass"

        def test_burns_cpu_in_a_child():
            subprocess.run([sys.executable, "-c", _BURN], check=True)
        """,
}


@pytest.fixture(scope="module")
def scratch_run(tmp_path_factory: pytest.TempPathFactory) -> MeasuredRun:
    root = tmp_path_factory.mktemp("file-cost")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for name, body in _SCRATCH_FILES.items():
        (root / name).write_text(textwrap.dedent(body), encoding="utf-8")
    run = run_pytest_measured(
        ["-c", str(root / "pytest.ini"), "-p", "no:django", "-p", "no:cacheprovider", "-q", str(root)],
        cwd=root,
        timeout_s=60,
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    return run


class TestMeasurementSeesWorkAndNotWaiting:
    def test_a_file_that_only_sleeps_costs_wall_time_and_almost_no_cpu(self, scratch_run: MeasuredRun) -> None:
        cost = scratch_run.costs["test_sleeps.py"]
        assert cost.wall_s >= 0.9
        assert cost.cpu_s < 0.25

    def test_a_file_that_burns_cpu_is_charged_that_cpu(self, scratch_run: MeasuredRun) -> None:
        assert scratch_run.costs["test_burns_cpu.py"].cpu_s >= 0.7

    def test_cpu_burnt_in_a_reaped_subprocess_is_charged_to_the_file_that_ran_it(
        self, scratch_run: MeasuredRun
    ) -> None:
        assert scratch_run.costs["test_burns_cpu_in_a_child.py"].cpu_s >= 0.7

    def test_each_file_is_charged_under_its_own_name(self, scratch_run: MeasuredRun) -> None:
        assert set(scratch_run.costs) == set(_SCRATCH_FILES)

    def test_the_budget_flags_the_cpu_heavy_files_and_spares_the_sleeper(self, scratch_run: MeasuredRun) -> None:
        assert set(over_budget(scratch_run.costs, _SCALED_BUDGET_S)) == {
            "test_burns_cpu.py",
            "test_burns_cpu_in_a_child.py",
        }


class TestBudgetPredicateAtTheRealLimit:
    @pytest.mark.parametrize(
        ("cost", "flagged"),
        [
            (FileCost(cpu_s=20.0, wall_s=20.0), True),
            (FileCost(cpu_s=0.05, wall_s=20.0), False),
            (FileCost(cpu_s=15.01, wall_s=15.01), True),
            (FileCost(cpu_s=15.0, wall_s=60.0), False),
        ],
    )
    def test_the_limit_applies_to_cpu_seconds_only(self, cost: FileCost, *, flagged: bool) -> None:
        assert bool(over_budget({"tests/conformance/test_scratch.py": cost}, 15.0)) is flagged

    def test_the_failure_message_carries_both_cpu_and_wall(self) -> None:
        costs = {"tests/conformance/test_scratch.py": FileCost(cpu_s=16.5, wall_s=20.0)}
        assert describe(costs) == "tests/conformance/test_scratch.py: 16.5s CPU (20.0s wall)"


class TestUnmeasuredFiles:
    _COST = FileCost(cpu_s=1.0, wall_s=1.0)

    @pytest.mark.parametrize(
        ("declared", "measured", "missing"),
        [
            pytest.param(["b.py", "c.py", "a.py"], ["c.py"], ["a.py", "b.py"], id="reported-and-sorted"),
            pytest.param(["a.py", "b.py"], ["a.py", "b.py"], [], id="all-measured"),
            pytest.param(["a.py"], ["b.py"], ["a.py"], id="another-files-cost-does-not-cover-it"),
            pytest.param([], ["a.py"], [], id="nothing-declared"),
        ],
    )
    def test_reports_the_declared_files_with_no_recorded_cost(
        self, declared: list[str], measured: list[str], missing: list[str]
    ) -> None:
        assert unmeasured(declared, dict.fromkeys(measured, self._COST)) == missing


class TestSettleTakesTheLowestCpuOfTwoRuns:
    @pytest.mark.parametrize(
        ("first_cpu_s", "second_cpu_s", "flagged"),
        [
            pytest.param(20.0, 4.0, False, id="one-run-spike-is-cleared"),
            pytest.param(20.0, 21.0, True, id="heavy-in-both-runs"),
            pytest.param(20.0, None, True, id="missing-from-the-second-run-is-not-cleared"),
            pytest.param(16.0, 15.0, False, id="exactly-the-limit-in-the-second-run-is-cleared"),
            pytest.param(16.0, 15.01, True, id="just-over-the-limit-in-the-second-run"),
            pytest.param(15.0, 20.0, False, id="exactly-the-limit-in-the-first-run-is-no-offender"),
            pytest.param(1.0, 20.0, False, id="a-later-spike-never-turns-a-clean-file-red"),
        ],
    )
    def test_a_file_is_flagged_only_when_its_lowest_reading_is_over_the_limit(
        self, first_cpu_s: float, second_cpu_s: float | None, *, flagged: bool
    ) -> None:
        first = {"tests/conformance/test_scratch.py": FileCost(cpu_s=first_cpu_s, wall_s=60.0)}
        second = (
            {}
            if second_cpu_s is None
            else {"tests/conformance/test_scratch.py": FileCost(cpu_s=second_cpu_s, wall_s=60.0)}
        )
        assert bool(settle(first, second, 15.0)) is flagged

    def test_the_failure_message_carries_cpu_and_wall_of_both_runs(self) -> None:
        first = {"a.py": FileCost(cpu_s=16.5, wall_s=20.0), "b.py": FileCost(cpu_s=18.0, wall_s=19.0)}
        second = {"a.py": FileCost(cpu_s=15.5, wall_s=17.0)}
        assert describe_runs(first, second) == (
            "a.py: 16.5s CPU (20.0s wall), re-run 15.5s CPU (17.0s wall); "
            "b.py: 18.0s CPU (19.0s wall), re-run not measured"
        )


_SCRATCH_CONFTEST = """
    from pathlib import Path

    def pytest_runtest_setup(item):
        with Path("runs.log").open("a", encoding="utf-8") as log:
            log.write(item.nodeid.split("::", 1)[0] + "\\n")
    """

_BURN_ONLY_ON_THE_FIRST_RUN = """
    import time
    from pathlib import Path

    def test_burns_only_on_its_first_run():
        first_run = not Path("burnt").exists()
        Path("burnt").touch()
        if first_run:
            start = time.process_time()
            while time.process_time() - start < 0.8:
                pass
    """

_BURN_ON_EVERY_RUN = """
    import time

    def test_burns_on_every_run():
        start = time.process_time()
        while time.process_time() - start < 0.8:
            pass
    """

_SCRATCH_FILES_FOR_REMEASURING = {
    "test_burns_only_on_its_first_run.py": _BURN_ONLY_ON_THE_FIRST_RUN,
    "test_burns_on_every_run.py": _BURN_ON_EVERY_RUN,
    "test_light.py": "def test_light():\n    pass\n",
    "test_collects_nothing.py": "def helper():\n    pass\n",
}


def _scratch_dir(root: Path, files: Mapping[str, str]) -> Path:
    root.mkdir(exist_ok=True)
    (root / "pytest.ini").write_text("[pytest]\naddopts = -p no:django -p no:cacheprovider -q\n", encoding="utf-8")
    (root / "conftest.py").write_text(textwrap.dedent(_SCRATCH_CONFTEST), encoding="utf-8")
    for name, body in files.items():
        (root / name).write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def _files_run(scratch: Path) -> Counter[str]:
    return Counter((scratch / "runs.log").read_text(encoding="utf-8").split())


@dataclass(frozen=True, slots=True)
class _Remeasured:
    scratch: Path
    first: MeasuredRun
    rerun: Mapping[str, FileCost]
    settled: Mapping[str, FileCost]


@pytest.fixture(scope="module")
def remeasured(tmp_path_factory: pytest.TempPathFactory) -> _Remeasured:
    scratch = _scratch_dir(tmp_path_factory.mktemp("remeasure"), _SCRATCH_FILES_FOR_REMEASURING)
    first = run_pytest_measured(list(_SCRATCH_FILES_FOR_REMEASURING), cwd=scratch, timeout_s=30)
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    rerun = remeasure_offenders(first.costs, budget_s=_SCALED_BUDGET_S, cwd=scratch, timeout_s=30)
    return _Remeasured(scratch, first, rerun, settle(first.costs, rerun, _SCALED_BUDGET_S))


class TestOffendersAreMeasuredOnceMoreInAFreshChild:
    def test_a_file_over_budget_only_on_its_first_run_is_cleared(self, remeasured: _Remeasured) -> None:
        spiker = "test_burns_only_on_its_first_run.py"
        assert spiker in over_budget(remeasured.first.costs, _SCALED_BUDGET_S)
        assert spiker not in remeasured.settled

    def test_a_file_over_budget_in_every_run_is_flagged(self, remeasured: _Remeasured) -> None:
        assert set(remeasured.settled) == {"test_burns_on_every_run.py"}

    def test_only_the_files_over_budget_are_run_again(self, remeasured: _Remeasured) -> None:
        assert set(remeasured.rerun) == {"test_burns_only_on_its_first_run.py", "test_burns_on_every_run.py"}
        assert _files_run(remeasured.scratch) == {
            "test_light.py": 1,
            "test_burns_only_on_its_first_run.py": 2,
            "test_burns_on_every_run.py": 2,
        }

    def test_a_declared_file_that_ran_no_test_has_no_cost(self, remeasured: _Remeasured) -> None:
        assert unmeasured(list(_SCRATCH_FILES_FOR_REMEASURING), remeasured.first.costs) == ["test_collects_nothing.py"]

    def test_nothing_is_run_again_when_no_file_is_over_budget(self, tmp_path: Path) -> None:
        scratch = _scratch_dir(tmp_path / "calm", {"test_light.py": "def test_light():\n    pass\n"})
        first = run_pytest_measured(["test_light.py"], cwd=scratch, timeout_s=30)
        assert remeasure_offenders(first.costs, budget_s=_SCALED_BUDGET_S, cwd=scratch, timeout_s=30) == {}
        assert _files_run(scratch) == {"test_light.py": 1}

    @pytest.mark.parametrize(
        ("second_run", "rerun_timeout_s"),
        [
            pytest.param('raise AssertionError("flaky")', 30, id="fails"),
            pytest.param("time.sleep(30)", 3, id="hangs"),
        ],
    )
    def test_a_rerun_that_fails_or_hangs_leaves_the_file_flagged(
        self, tmp_path: Path, second_run: str, rerun_timeout_s: float
    ) -> None:
        body = f"""
            import time
            from pathlib import Path

            def test_burns_then_misbehaves():
                if not Path("burnt").exists():
                    Path("burnt").touch()
                    start = time.process_time()
                    while time.process_time() - start < 0.8:
                        pass
                else:
                    {second_run}
            """
        scratch = _scratch_dir(tmp_path / "flaky", {"test_misbehaves_on_rerun.py": body})
        first = run_pytest_measured(["test_misbehaves_on_rerun.py"], cwd=scratch, timeout_s=30)
        assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
        rerun = remeasure_offenders(first.costs, budget_s=_SCALED_BUDGET_S, cwd=scratch, timeout_s=rerun_timeout_s)
        assert rerun == {}
        assert set(settle(first.costs, rerun, _SCALED_BUDGET_S)) == {"test_misbehaves_on_rerun.py"}
