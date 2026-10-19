# test-path: cross-cutting — executes the eval*.yml loop bodies; no single src/teatree/ mirror.
"""A usage limit parks an eval lane; it never reds it, retries into it, or keeps spending.

Each test runs the SHIPPED step body under bash with a stub ``uv`` on ``PATH``: ``uv run
t3 eval run NAME`` logs NAME and exits with the code the test scripted for it, and the
redacting ``tee`` filter passes its input through. Exit 75 is the eval run's "the
subscription usage limit refused it" code.
"""

import dataclasses
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from teatree.eval.owed import OwedLedger

_BASH = shutil.which("bash") or "/bin/bash"
_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_PR_LOOP = "Metered behavioral eval — the changed scenarios (in Docker)"
_NIGHTLY_OWED = "Owed scenarios (in Docker)"
_NIGHTLY_SHARD = "Nightly smoke behavioral eval (in Docker)"
_WEEKLY_LEG = "Behavioral eval suite (in Docker; pass@k; --require-executed always armed)"
_BENCHMARK_LEG = "Metered behavioral benchmark suite (in Docker; 3-tier matrix; --require-executed always armed)"
_SELECTOR = 'uv run python scripts/eval/select_oauth.py || [ "$?" -eq 75 ]'
_LIMIT = 75

_STUB_UV = r"""#!/bin/bash
if [ "$1 $2 $3 $4" = "run t3 eval run" ]; then
    name="$5"
    case "$name" in --*) name="suite" ;; esac
    echo "$name" >> "$STUB_LOG"
    rc_var="STUB_RC_$name"
    exit "${!rc_var:-0}"
fi
if [ "$1 $2 $3 $4 $5" = "run t3 eval owed next" ]; then
    printf '%s\n' $STUB_OWED
    exit 0
fi
if [ "$1 $2 $3" = "run python scripts/eval/select_oauth.py" ]; then
    exit "${STUB_RC_selector:-0}"
fi
if [ "$1 $2 $3 $4 $5" = "run python -m teatree.eval.artifact_redaction tee" ]; then
    if [ -n "$6" ]; then tee -a "$6"; else cat; fi
    exit 0
fi
echo "unexpected uv $*" >&2
exit 99
"""


def _workflow(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8")))


def _step(workflow: str, job: str, step_name: str) -> dict[str, Any]:
    steps = cast("list[dict[str, Any]]", _workflow(workflow)["jobs"][job]["steps"])
    return next(step for step in steps if step.get("name") == step_name)


def _script(workflow: str, job: str, step_name: str) -> str:
    step = _step(workflow, job, step_name)
    return cast("str", step.get("run") or step["with"]["command"])


@dataclasses.dataclass(frozen=True)
class _Run:
    returncode: int
    invoked: list[str]
    temp: Path

    def lines(self, name: str) -> list[str]:
        path = self.temp / name
        return path.read_text(encoding="utf-8").split() if path.exists() else []


@pytest.fixture
def shell(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_STUB_UV, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    log = tmp_path / "invoked.log"

    def run(script: str, *, rcs: dict[str, int] | None = None, **env: str) -> _Run:
        environment = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
            "RUNNER_TEMP": str(temp),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "step-summary"),
            "STUB_LOG": str(log),
            **{f"STUB_RC_{name}": str(code) for name, code in (rcs or {}).items()},
            **env,
        }
        result = subprocess.run([_BASH, "-c", script], env=environment, capture_output=True, text=True, check=False)
        invoked = log.read_text(encoding="utf-8").split() if log.exists() else []
        return _Run(result.returncode, invoked, temp)

    return run


_PR_WORKFLOWS = ["eval-pr.yml", "eval-pr-reusable.yml"]


@pytest.mark.parametrize("workflow", _PR_WORKFLOWS)
class TestPrLaneParksOnTheLimit:
    def test_the_limit_stops_the_loop_and_owes_the_rest(self, shell, workflow: str) -> None:
        run = shell(_script(workflow, "eval", _PR_LOOP), rcs={"beta": _LIMIT}, EVAL_NAMES="alpha,beta,gamma")
        assert run.returncode == 0
        assert run.invoked == ["alpha", "beta"]
        assert run.lines("eval-owed-limit.txt") == ["beta", "gamma"]

    def test_a_confirmed_red_before_the_limit_still_reds(self, shell, workflow: str) -> None:
        run = shell(
            _script(workflow, "eval", _PR_LOOP), rcs={"alpha": 1, "beta": _LIMIT}, EVAL_NAMES="alpha,beta,gamma"
        )
        assert run.returncode == 1
        assert run.lines("eval-owed-limit.txt") == ["beta", "gamma"]

    def test_a_retry_after_the_limit_invokes_nothing(self, shell, workflow: str) -> None:
        script = _script(workflow, "eval", _PR_LOOP)
        first = shell(script, rcs={"alpha": 1, "beta": _LIMIT}, EVAL_NAMES="alpha,beta")
        retry = shell(script, EVAL_NAMES="alpha,beta")
        assert first.invoked == retry.invoked == ["alpha", "beta"]
        assert retry.returncode == 1

    def test_a_red_on_an_earlier_attempt_beats_a_later_limit(self, shell, workflow: str) -> None:
        script = _script(workflow, "eval", _PR_LOOP)
        first = shell(script, rcs={"alpha": 1}, EVAL_NAMES="alpha,beta")
        later = shell(script, rcs={"beta": _LIMIT}, EVAL_NAMES="alpha,beta")
        assert first.returncode == later.returncode == 1
        assert later.lines("eval-owed-limit.txt") == ["beta"]

    def test_no_limit_owes_nothing(self, shell, workflow: str) -> None:
        run = shell(_script(workflow, "eval", _PR_LOOP), EVAL_NAMES="alpha,beta")
        assert run.returncode == 0
        assert run.invoked == ["alpha", "beta"]
        assert run.lines("eval-owed-limit.txt") == []


class TestNightlyOwedBatch:
    def test_a_red_on_an_earlier_attempt_beats_a_later_limit(self, shell) -> None:
        script = _script("eval-nightly.yml", "eval", _NIGHTLY_OWED)
        first = shell(script, rcs={"alpha": 1, "beta": 1}, STUB_OWED="alpha beta")
        later = shell(script, rcs={"beta": _LIMIT}, STUB_OWED="alpha beta")
        assert first.returncode == later.returncode == 1

    def test_a_pass_clears_and_the_limit_leaves_the_rest_owed(self, shell) -> None:
        script = _script("eval-nightly.yml", "eval", _NIGHTLY_OWED)
        run = shell(script, rcs={"beta": _LIMIT}, STUB_OWED="alpha beta gamma")
        assert run.returncode == 0
        assert run.invoked == ["alpha", "beta"]
        assert run.lines("eval-owed-passed.txt") == ["alpha"]
        assert (run.temp / "eval-limit-hit").exists()

    def test_a_confirmed_red_stays_owed_and_reds(self, shell) -> None:
        run = shell(_script("eval-nightly.yml", "eval", _NIGHTLY_OWED), rcs={"alpha": 1}, STUB_OWED="alpha beta")
        assert run.returncode == 1
        assert run.lines("eval-owed-passed.txt") == ["beta"]

    def test_the_shard_does_not_run_after_the_limit(self, shell) -> None:
        shell(_script("eval-nightly.yml", "eval", _NIGHTLY_OWED), rcs={"alpha": _LIMIT}, STUB_OWED="alpha")
        shard = shell(_script("eval-nightly.yml", "eval", _NIGHTLY_SHARD))
        assert shard.returncode == 0
        assert shard.invoked == ["alpha"]


@pytest.mark.parametrize(
    ("workflow", "step_name"),
    [("eval-nightly.yml", _NIGHTLY_SHARD), ("eval.yml", _WEEKLY_LEG)],
    ids=["nightly", "weekly"],
)
class TestSingleInvocationLanesParkOnTheLimit:
    def test_a_red_on_an_earlier_attempt_beats_a_later_limit(self, shell, workflow: str, step_name: str) -> None:
        script = _script(workflow, "eval", step_name)
        first = shell(script, rcs={"suite": 1}, EVAL_SUFFIX="clean_room-1-2")
        later = shell(script, rcs={"suite": _LIMIT}, EVAL_SUFFIX="clean_room-1-2")
        replay = shell(script, EVAL_SUFFIX="clean_room-1-2")
        assert first.returncode == later.returncode == replay.returncode == 1
        assert replay.invoked == ["suite", "suite"]

    def test_the_limit_parks_green(self, shell, workflow: str, step_name: str) -> None:
        run = shell(_script(workflow, "eval", step_name), rcs={"suite": _LIMIT}, EVAL_SUFFIX="clean_room-1-2")
        assert run.returncode == 0
        assert run.invoked == ["suite"]

    def test_a_red_stays_red(self, shell, workflow: str, step_name: str) -> None:
        run = shell(_script(workflow, "eval", step_name), rcs={"suite": 1}, EVAL_SUFFIX="clean_room-1-2")
        assert run.returncode == 1


@pytest.mark.parametrize(
    ("workflow", "job"),
    [("eval-pr.yml", "eval"), ("eval-nightly.yml", "eval"), ("eval.yml", "eval"), ("eval.yml", "prepare")],
)
class TestTheSelectorParksInsteadOfFailing:
    def _selector(self, workflow: str, job: str) -> dict[str, Any]:
        steps = cast("list[dict[str, Any]]", _workflow(workflow)["jobs"][job]["steps"])
        return next(step for step in steps if step.get("run") == _SELECTOR)

    @pytest.mark.parametrize(("selector_rc", "step_rc"), [(0, 0), (_LIMIT, 0), (1, 1)])
    def test_exit_75_passes_the_step_and_any_other_failure_fails_it(
        self, shell, workflow: str, job: str, selector_rc: int, step_rc: int
    ) -> None:
        run = shell(f"set -e\n{self._selector(workflow, job)['run']}", rcs={"selector": selector_rc})
        assert run.returncode == step_rc


@pytest.mark.parametrize("workflow", ["eval.yml", "eval-nightly.yml"])
def test_a_scheduled_lane_never_falls_back_to_the_metered_key(workflow: str) -> None:
    envs = [step.get("env", {}) for job in _workflow(workflow)["jobs"].values() for step in job.get("steps", [])]
    assert not [env for env in envs if "EVAL_API_KEY_FALLBACK" in env]


def test_the_pr_lane_keeps_its_owner_approved_metered_fallback() -> None:
    selector = _step("eval-pr.yml", "eval", "Select the freshest eval OAuth account")
    assert selector["env"]["EVAL_API_KEY_FALLBACK"] == "${{ vars.EVAL_API_KEY_FALLBACK }}"


@pytest.mark.parametrize(
    ("workflow", "step_name"),
    [("eval-pr.yml", _PR_LOOP), ("eval-nightly.yml", _NIGHTLY_OWED), ("eval.yml", _WEEKLY_LEG)],
)
def test_a_parked_selector_skips_the_eval(workflow: str, step_name: str) -> None:
    assert "steps.select.outputs.parked != 'true'" in _step(workflow, "eval", step_name)["if"]


def test_the_weekly_sweep_runs_one_leg_at_a_time_and_never_overlaps() -> None:
    workflow = _workflow("eval.yml")
    assert workflow["jobs"]["eval"]["strategy"]["max-parallel"] == 1
    assert workflow["concurrency"] == {"group": "eval-weekly-sweep", "cancel-in-progress": False}


def test_a_sweep_whose_every_leg_parked_publishes_no_empty_dashboard(shell) -> None:
    run = shell(_script("eval.yml", "publish", "Merge the shard summaries into the public dashboard"))
    assert run.returncode == 0


def test_the_benchmark_never_overlaps() -> None:
    assert _workflow("eval-benchmark.yml")["concurrency"] == {"group": "eval-benchmark", "cancel-in-progress": False}


class TestBenchmarkCapacityPark:
    def test_selector_park_uploads_a_marker(self, shell) -> None:
        step = _step("eval-weekly-reusable.yml", "eval", "Mark a benchmark parked by the reserve")
        upload = _step("eval-weekly-reusable.yml", "eval", "Upload the benchmark matrix HTML dashboard")
        assert step["if"] == "steps.select.outputs.parked == 'true'"
        run = shell(step["run"], SUFFIX="clean_room-1")
        assert run.returncode == 0
        assert (run.temp / "eval-benchmark-clean_room-1.parked").read_text(encoding="utf-8") == "parked\n"
        assert ".parked" in upload["with"]["path"]

    def test_limit_parks_and_retry_invokes_nothing(self, shell) -> None:
        script = _script("eval-weekly-reusable.yml", "eval", _BENCHMARK_LEG)
        first = shell(script, rcs={"suite": _LIMIT}, EVAL_SUFFIX="clean_room-1")
        retry = shell(script, EVAL_SUFFIX="clean_room-1")
        assert first.returncode == retry.returncode == 0
        assert first.invoked == retry.invoked == ["suite"]
        assert (first.temp / "eval-limit-hit-clean_room-1").exists()

    def test_earlier_measured_red_beats_a_later_limit(self, shell) -> None:
        script = _script("eval-weekly-reusable.yml", "eval", _BENCHMARK_LEG)
        first = shell(script, rcs={"suite": 1}, EVAL_SUFFIX="clean_room-1")
        second = shell(script, rcs={"suite": _LIMIT}, EVAL_SUFFIX="clean_room-1")
        assert first.returncode == second.returncode == 1
        assert second.invoked == ["suite", "suite"]

    def test_parked_artifact_skips_publish_without_a_dashboard(self, shell, tmp_path: Path) -> None:
        marker = tmp_path / "runner-temp" / "benchmarks" / "eval-benchmark-clean_room-1.parked"
        marker.parent.mkdir()
        marker.write_text("parked\n", encoding="utf-8")
        script = _script(
            "eval-weekly-reusable.yml", "publish", "Refuse to publish an incomplete or unmetered dashboard"
        )
        result = shell(script, GITHUB_OUTPUT=str(tmp_path / "output"), EVAL_MATRIX='[{"lane":"clean_room"}]')
        assert result.returncode == 0
        assert result.invoked == []
        assert (tmp_path / "output").read_text(encoding="utf-8") == "publish=false\n"


def test_the_nightly_runs_owed_scenarios_before_the_shard() -> None:
    names = [step.get("name") for step in _workflow("eval-nightly.yml")["jobs"]["eval"]["steps"]]
    assert names.index(_NIGHTLY_OWED) < names.index(_NIGHTLY_SHARD)


_RECORD_OWED = "Record the owed scenarios"
_REAL_T3_UV = f"""#!/bin/bash
[ "$1 $2" = "run t3" ] || exit 99
shift 2
exec "{sys.executable}" -c 'import sys; from t3_bootstrap import main; sys.argv[0] = "t3"; sys.exit(main())' "$@"
"""


@pytest.fixture
def record_owed(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_REAL_T3_UV, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    output = tmp_path / "github-output"

    def run(*, parked: str = "", limit: tuple[str, ...] = (), deferred: str = "") -> dict[str, str]:
        if limit:
            (temp / "eval-owed-limit.txt").write_text("".join(f"{name}\n" for name in limit), encoding="utf-8")
        environment = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(temp),
            "GITHUB_OUTPUT": str(output),
            "XDG_DATA_HOME": str(tmp_path / "xdg"),
            "SELECTOR_PARKED": parked,
            "EVAL_NAMES": "alpha,beta",
            "DEFERRED": deferred,
            "SOURCE": "pr#7@abc123",
        }
        result = subprocess.run(
            [_BASH, "-c", _script("eval-pr.yml", "eval", _RECORD_OWED)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            cwd=_WORKFLOWS.parents[1],
        )
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())

    return run, temp / "eval-owed.json"


class TestThePrLaneRecordsWhatItOwes:
    def test_a_parked_selector_owes_every_changed_scenario(self, record_owed) -> None:
        run, ledger = record_owed
        assert run(parked="true") == {"parked": "true", "owed": "alpha,beta"}
        assert "pr#7@abc123" in ledger.read_text(encoding="utf-8")

    def test_the_limit_and_the_cap_both_owe_and_only_the_limit_parks(self, record_owed) -> None:
        run, _ = record_owed
        assert run(limit=("beta",), deferred="gamma") == {"parked": "true", "owed": "beta,gamma"}

    def test_a_deferred_tail_is_owed_without_parking(self, record_owed) -> None:
        run, _ = record_owed
        assert run(deferred="gamma") == {"parked": "false", "owed": "gamma"}

    def test_nothing_owed_uploads_an_empty_ledger(self, record_owed) -> None:
        run, ledger = record_owed
        assert run() == {"parked": "false", "owed": ""}
        assert OwedLedger.from_json(ledger.read_text(encoding="utf-8")) == OwedLedger()


@pytest.mark.parametrize("workflow", ["eval-pr.yml", "eval-nightly.yml"])
def test_every_owed_ledger_step_runs_after_the_control_db_sandbox(workflow: str) -> None:
    for job in _workflow(workflow)["jobs"].values():
        runs = [str(step.get("run", "")) for step in job.get("steps", [])]
        owed = [index for index, run in enumerate(runs) if "t3 eval owed" in run]
        if owed:
            sandbox = next((index for index, run in enumerate(runs) if "XDG_DATA_HOME=" in run), len(runs))
            assert sandbox < owed[0]
