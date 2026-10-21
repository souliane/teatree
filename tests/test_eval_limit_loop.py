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
if [ "$1 $2" = "run t3" ]; then echo "$*" >> "$STUB_T3_LOG"; fi
if [ "$1 $2 $3 $4" = "run t3 eval run" ]; then
    name="$5"
    case "$name" in --*) name="suite" ;; esac
    echo "$name" >> "$STUB_LOG"
    transcript=""; summary=""; rows=""; docker=false
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --docker) docker=true ;;
            --transcript-html) transcript="$2"; shift ;;
            --summary-md) summary="$2"; shift ;;
            --summary-json) rows="$2"; shift ;;
        esac
        shift
    done
    if [ "$docker" = true ] && [ -n "$transcript" ] && [ -n "$summary" ] && [ -n "$rows" ]; then
        [ "$(dirname "$transcript")" = "$(dirname "$summary")" ] || exit 2
        [ "$(dirname "$summary")" = "$(dirname "$rows")" ] || exit 2
    fi
    rc_var="STUB_RC_$name"
    rc="${!rc_var:-0}"
    [ "$rc" -ne 0 ] || [ -z "$summary" ] || printf '| %s | PASS |\n' "$name" > "$summary"
    [ "$rc" -ne 0 ] || [ -z "$rows" ] || printf '{"scenario":"%s"}\n' "$name" > "$rows"
    exit "$rc"
fi
if [ "$1 $2 $3 $4" = "run t3 eval list" ]; then
    [ -z "${STUB_NAMES:-}" ] || printf '%s\n' $STUB_NAMES
    exit 0
fi
if [ "$1 $2 $3 $4 $5" = "run t3 eval owed next" ]; then
    printf '%s\n' $STUB_OWED
    exit 0
fi
if [ "$1 $2 $3 $4 $5" = "run t3 eval owed record" ]; then
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "--names-file" ]; then cat "$2" >> "$RUNNER_TEMP/recorded-owed.txt"; exit "${STUB_RC_owed:-0}"; fi
        shift
    done
    exit 99
fi
if [ "$1 $2 $3 $4" = "run t3 eval merge-summary-json" ]; then
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
    t3_invoked: list[str]
    temp: Path
    stderr: str

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
    t3_log = tmp_path / "t3-invoked.log"

    def run(script: str, *, rcs: dict[str, int] | None = None, **env: str) -> _Run:
        environment = {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
            "RUNNER_TEMP": str(temp),
            "HEAL_DIR": str(temp / "heal"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "step-summary"),
            "GITHUB_RUN_ID": "7",
            "GITHUB_SHA": "abc123",
            "STUB_LOG": str(log),
            "STUB_T3_LOG": str(t3_log),
            **{f"STUB_RC_{name}": str(code) for name, code in (rcs or {}).items()},
            **env,
        }
        result = subprocess.run([_BASH, "-c", script], env=environment, capture_output=True, text=True, check=False)
        invoked = log.read_text(encoding="utf-8").split() if log.exists() else []
        t3_invoked = t3_log.read_text(encoding="utf-8").splitlines() if t3_log.exists() else []
        return _Run(result.returncode, invoked, t3_invoked, temp, result.stderr)

    return run


_PR_WORKFLOWS = ["eval-pr.yml", "eval-pr-reusable.yml"]


@pytest.mark.parametrize("workflow", _PR_WORKFLOWS)
class TestPrLaneParksOnTheLimit:
    def test_a_confirmed_red_before_the_limit_still_reds(self, shell, workflow: str) -> None:
        run = shell(
            _script(workflow, "eval", _PR_LOOP), rcs={"alpha": 1, "beta": _LIMIT}, EVAL_NAMES="alpha,beta,gamma"
        )
        assert run.returncode == 1
        assert run.lines("eval-owed-limit.txt") == ["beta", "gamma"]

    def test_a_retry_after_only_a_limit_invokes_nothing(self, shell, workflow: str) -> None:
        script = _script(workflow, "eval", _PR_LOOP)
        first = shell(script, rcs={"beta": _LIMIT}, EVAL_NAMES="alpha,beta,gamma")
        retry = shell(script, EVAL_NAMES="alpha,beta,gamma")

        assert first.returncode == retry.returncode == 0
        assert first.invoked == retry.invoked == ["alpha", "beta"]
        assert first.lines("eval-owed-limit.txt") == ["beta", "gamma"]

    def test_no_limit_owes_nothing(self, shell, workflow: str) -> None:
        run = shell(_script(workflow, "eval", _PR_LOOP), EVAL_NAMES="alpha,beta")
        assert run.returncode == 0
        assert run.invoked == ["alpha", "beta"]
        assert run.lines("eval-owed-limit.txt") == []


class TestNightlyOwedBatch:
    def test_a_pass_clears_and_the_limit_leaves_the_rest_owed(self, shell) -> None:
        script = _script("eval-nightly.yml", "eval", _NIGHTLY_OWED)
        run = shell(script, rcs={"beta": _LIMIT}, STUB_OWED="alpha beta gamma")
        retry = shell(script, STUB_OWED="alpha beta gamma")
        assert run.returncode == 0
        assert retry.returncode == 0
        assert run.invoked == retry.invoked == ["alpha", "beta"]
        assert run.lines("eval-owed-passed.txt") == ["alpha"]
        assert run.lines("eval-owed-attempted.txt") == ["alpha", "beta"]
        assert (run.temp / "eval-limit-hit").exists()

    def test_the_shard_does_not_run_after_the_limit(self, shell) -> None:
        shell(_script("eval-nightly.yml", "eval", _NIGHTLY_OWED), rcs={"alpha": _LIMIT}, STUB_OWED="alpha")
        shard = shell(_script("eval-nightly.yml", "eval", _NIGHTLY_SHARD))
        assert shard.returncode == 0
        assert shard.invoked == ["alpha"]


def test_the_nightly_shard_limit_parks_green(shell) -> None:
    run = shell(_script("eval-nightly.yml", "eval", _NIGHTLY_SHARD), rcs={"suite": _LIMIT})

    assert run.returncode == 0
    assert run.invoked == ["suite"]
    assert not (run.temp / "eval-limit-hit").exists()


_WEEKLY_AND_HEAL_LEGS = pytest.mark.parametrize(
    ("workflow", "step_name", "env"),
    [
        (
            "eval.yml",
            _WEEKLY_LEG,
            {
                "STUB_NAMES": "alpha beta gamma",
                "EVAL_SUFFIX": "clean_room-1-2",
                "EVAL_LANE": "clean_room",
                "EVAL_SHARD": "1/2",
            },
        ),
        (
            "eval-ci-heal.yml",
            "Behavioral eval — one shard (in Docker)",
            {"EVAL_SCENARIOS": "alpha,beta,gamma", "SUFFIX": "subset"},
        ),
    ],
    ids=["weekly", "heal"],
)


@_WEEKLY_AND_HEAL_LEGS
def test_weekly_and_heal_owe_the_cutoff_and_never_retry_it(
    shell, workflow: str, step_name: str, env: dict[str, str]
) -> None:
    script = _script(workflow, "eval", step_name)
    first = shell(script, rcs={"beta": _LIMIT}, **env)
    assert first.returncode == 0
    assert first.invoked == ["alpha", "beta"]
    assert first.lines("recorded-owed.txt") == ["beta", "gamma"]
    assert any("eval owed record" in call for call in first.t3_invoked)
    if workflow == "eval.yml":
        assert any("eval list --lane clean_room --shard 1/2 --names-only" in call for call in first.t3_invoked)
        assert any("eval merge-summary-json" in call for call in first.t3_invoked)
    retry = shell(script, **env)
    assert retry.returncode == 0
    assert retry.invoked == first.invoked
    assert retry.t3_invoked == first.t3_invoked


@_WEEKLY_AND_HEAL_LEGS
def test_a_failed_owed_record_stays_failed_and_never_retries_into_the_limit(
    shell, workflow: str, step_name: str, env: dict[str, str]
) -> None:
    script = _script(workflow, "eval", step_name)
    first = shell(script, rcs={"beta": _LIMIT, "owed": 3}, **env)
    retry = shell(script, **env)
    assert first.returncode == retry.returncode == 3
    assert retry.invoked == first.invoked == ["alpha", "beta"]


@pytest.mark.parametrize(("names", "expected_rc"), [("alpha beta", 0), ("", 1)])
def test_weekly_retry_replaces_the_previous_attempts_summary(shell, names: str, expected_rc: int) -> None:
    script = _script("eval.yml", "eval", _WEEKLY_LEG)
    env = {"STUB_NAMES": names, "EVAL_SUFFIX": "clean_room-1-2", "EVAL_LANE": "clean_room", "EVAL_SHARD": "1/2"}
    first = shell(script, **env)
    second = shell(script, **env)
    summary = first.temp / "eval-summary-clean_room-1-2.md"
    assert first.returncode == second.returncode == expected_rc
    if names:
        assert summary.read_text(encoding="utf-8").splitlines() == ["| alpha | PASS |", "| beta | PASS |"]
    else:
        assert first.invoked == []


_RETRIED_BODIES = [
    pytest.param("eval-pr.yml", _PR_LOOP, {"alpha": 1}, {"EVAL_NAMES": "alpha,beta"}, id="eval-pr"),
    pytest.param("eval-pr-reusable.yml", _PR_LOOP, {"alpha": 1}, {"EVAL_NAMES": "alpha,beta"}, id="eval-pr-reusable"),
    pytest.param("eval-nightly.yml", _NIGHTLY_OWED, {"alpha": 1}, {"STUB_OWED": "alpha beta"}, id="nightly-owed"),
    pytest.param("eval-nightly.yml", _NIGHTLY_SHARD, {"suite": 1}, {}, id="nightly-shard"),
    pytest.param(
        "eval.yml", _WEEKLY_LEG, {"suite": 1}, {"EVAL_SUFFIX": "clean_room-1-2", "STUB_NAMES": "suite"}, id="weekly-leg"
    ),
    pytest.param(
        "eval-weekly-reusable.yml", _BENCHMARK_LEG, {"suite": 1}, {"EVAL_SUFFIX": "clean_room-1"}, id="benchmark-leg"
    ),
]


@pytest.mark.parametrize(("workflow", "step_name", "red", "env"), _RETRIED_BODIES)
def test_a_measured_red_replays_without_running_the_retry(
    shell, workflow: str, step_name: str, red: dict[str, int], env: dict[str, str]
) -> None:
    script = _script(workflow, "eval", step_name)
    first = shell(script, rcs=red, **env)
    retry = shell(script, **env)
    assert first.returncode == retry.returncode == 1
    assert first.invoked
    assert retry.invoked == first.invoked
    if workflow in _PR_WORKFLOWS:
        assert retry.lines("eval-owed-limit.txt") == []
    if workflow == "eval-nightly.yml" and step_name == _NIGHTLY_OWED:
        assert retry.lines("eval-owed-passed.txt") == ["beta"]


@pytest.mark.parametrize("workflow", ["eval-pr.yml", "eval-nightly.yml", "eval.yml", "eval-ci-heal.yml"])
def test_each_selector_parks_on_75_and_fails_on_other_errors(shell, workflow: str) -> None:
    jobs = ("eval", "prepare") if workflow == "eval.yml" else ("eval",)
    for job in jobs:
        steps = cast("list[dict[str, Any]]", _workflow(workflow)["jobs"][job]["steps"])
        selector = next(step for step in steps if step.get("run") == _SELECTOR)
        script = f"set -e\n{selector['run']}"
        assert shell(script, rcs={"selector": _LIMIT}).returncode == 0
        assert shell(script, rcs={"selector": 1}).returncode == 1


@pytest.mark.parametrize("workflow", ["eval.yml", "eval-nightly.yml", "eval-weekly-reusable.yml"])
def test_a_subscription_lane_never_falls_back_to_the_metered_key(workflow: str) -> None:
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
    run = shell(
        _script("eval.yml", "publish", "Merge the shard summaries into the public dashboard"),
        EVAL_MATRIX='[{"lane":"clean_room","shard":"1/1"}]',
    )
    assert run.returncode == 0


def test_a_sweep_with_one_missing_leg_publishes_no_partial_dashboard(shell) -> None:
    summaries = shell(":").temp / "summaries"
    summaries.mkdir()
    (summaries / "eval-summary-one.md").write_text("| one | PASS |\n", encoding="utf-8")
    run = shell(
        _script("eval.yml", "publish", "Merge the shard summaries into the public dashboard"),
        EVAL_MATRIX='[{"lane":"clean_room","shard":"1/2"},{"lane":"clean_room","shard":"2/2"}]',
    )
    assert run.returncode == 0
    assert "merge_summaries.py" not in run.stderr


def test_the_benchmark_never_overlaps() -> None:
    assert _workflow("eval-benchmark.yml")["concurrency"] == {"group": "eval-benchmark", "cancel-in-progress": False}


class TestBenchmarkCapacityPark:
    def test_selector_park_uploads_a_marker(self, shell) -> None:
        step = _step("eval-weekly-reusable.yml", "eval", "Mark a benchmark parked by the reserve")
        upload = _step("eval-weekly-reusable.yml", "eval", "Upload the benchmark park marker")
        assert step["if"] == "steps.select.outputs.parked == 'true'"
        run = shell(step["run"], SUFFIX="clean_room-1")
        assert run.returncode == 0
        assert (run.temp / "eval-benchmark-clean_room-1.parked").read_text(encoding="utf-8") == "parked\n"
        assert upload["with"]["path"] == "${{ runner.temp }}/eval-benchmark-${{ steps.artifact.outputs.suffix }}.parked"
        assert upload["with"]["name"].startswith("eval-benchmark-")

    def test_limit_parks_and_retry_invokes_nothing(self, shell) -> None:
        script = _script("eval-weekly-reusable.yml", "eval", _BENCHMARK_LEG)
        first = shell(script, rcs={"suite": _LIMIT}, EVAL_SUFFIX="clean_room-1")
        retry = shell(script, EVAL_SUFFIX="clean_room-1")
        assert first.returncode == retry.returncode == 0
        assert first.invoked == retry.invoked == ["suite"]
        assert (first.temp / "eval-limit-hit-clean_room-1").exists()
        assert (first.temp / "eval-benchmark-clean_room-1.parked").read_text(encoding="utf-8") == "parked\n"

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
            **{key: value for key, value in os.environ.items() if key != "T3_REPO"},
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


def test_reusable_pr_record_step_writes_limit_debt(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_REAL_T3_UV, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    (temp / "eval-owed-limit.txt").write_text("limited\n", encoding="utf-8")
    script = _script("eval-pr-reusable.yml", "eval", "Record owed scenarios")
    script = script.replace("${{ github.event.pull_request.number }}", "7").replace(
        "${{ github.event.pull_request.head.sha }}", "abc123"
    )
    result = subprocess.run(
        [_BASH, "-c", script],
        env={
            **{key: value for key, value in os.environ.items() if key != "T3_REPO"},
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(temp),
            "XDG_DATA_HOME": str(tmp_path / "xdg"),
        },
        capture_output=True,
        text=True,
        check=False,
        cwd=_WORKFLOWS.parents[1],
    )
    assert result.returncode == 0, result.stderr
    ledger = OwedLedger.from_json((temp / "eval-owed.json").read_text(encoding="utf-8"))
    assert ledger.names == ("limited",)


def test_reusable_pr_reports_limit_debt_to_gate(tmp_path: Path, shell) -> None:
    (tmp_path / "eval-owed-limit.txt").write_text("beta\ngamma\n", encoding="utf-8")
    output = tmp_path / "github-output"

    result = shell(
        _script("eval-pr-reusable.yml", "eval", "Report the owed scenarios"),
        RUNNER_TEMP=str(tmp_path),
        GITHUB_OUTPUT=str(output),
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == ["parked=true", "owed=beta,gamma"]


@pytest.mark.parametrize("workflow", ["eval-pr.yml", "eval-nightly.yml"])
def test_every_owed_ledger_step_runs_after_the_control_db_sandbox(workflow: str) -> None:
    for job in _workflow(workflow)["jobs"].values():
        runs = [str(step.get("run", "")) for step in job.get("steps", [])]
        owed = [index for index, run in enumerate(runs) if "t3 eval owed" in run]
        if owed:
            sandbox = next((index for index, run in enumerate(runs) if "XDG_DATA_HOME=" in run), len(runs))
            assert sandbox < owed[0]
