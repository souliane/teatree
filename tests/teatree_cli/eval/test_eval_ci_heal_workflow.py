# test-path: cross-cutting — runs the eval-ci-heal.yml leg bodies then merges via teatree.eval.summary_json_merge.
"""Static contract checks for the ``eval-ci-heal`` workflow.

`eval-ci-heal.yml` is the workflow_dispatch-only workflow the CI-eval heal loop
dispatches. These pin its contract: the trigger + inputs, the reserve-gated OAuth
selector that parks a leg instead of spending into a usage limit, the no-silent-green
enforcement, that it reuses the exact in-Docker `t3 eval run --backend api
--docker --require-executed` invocation and emits the publish-safe `--summary-json`
artifact + the private transcript, and the injection-safe env-var pattern (no
caller input inlined into a run line).

The full suite (~231 scenarios) is FANNED OUT across a parallel matrix of shards
(souliane/teatree#3202): a `prepare` job computes the `{shard}` matrix, the `eval`
job runs one `--shard <index>/<total>` leg per matrix entry, and a `combine` job
merges every shard's per-scenario JSON into ONE `eval-heal-<sha>.json`. The eval
runs single-trial with `--escalate-on-fail` (the adaptive default) — the retired
blanket `--trials 2` pass@2 must be gone. These assertions FAIL if the sharding,
the combine job, or the adaptive single-trial invocation is missing. The park path
runs the shipped shell bodies under `bash -eo pipefail` against a stub `uv`.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from teatree.eval.summary_json_merge import merge_summary_json

_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "eval-ci-heal.yml"
_BASH = shutil.which("bash") or "/bin/bash"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_STUB_UV = """#!/bin/bash
shift
if [ "$1" = t3 ]; then
    calls=0
    [ -f "$T3_CALLS" ] && calls=$(wc -l < "$T3_CALLS")
    echo "$*" >> "$T3_CALLS"
    read -ra rcs <<< "$T3_RCS"
    exit "${rcs[$calls]:-${rcs[-1]}}"
fi
shift
exec "$REAL_PYTHON" "$@"
"""


def _load() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses the unquoted ``on:`` key as the boolean True.
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


def _jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", _load()["jobs"])


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


class TestTrigger:
    def test_is_workflow_dispatch_only(self) -> None:
        on = _on(_load())
        assert set(on) == {"workflow_dispatch"}

    def test_exposes_the_inputs(self) -> None:
        inputs = _on(_load())["workflow_dispatch"]["inputs"]
        assert set(inputs) == {"scenarios", "shards", "credential", "pr_ref"}
        assert inputs["credential"]["default"] == "subscription_oauth"

    def test_shards_input_has_a_sensible_default(self) -> None:
        inputs = _on(_load())["workflow_dispatch"]["inputs"]
        # A default in the recommended 6-8 range so each shard's worst case stays
        # comfortably under the per-step timeout.
        assert 6 <= int(inputs["shards"]["default"]) <= 8


def _eval_steps() -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _jobs()["eval"]["steps"])


def _step_index(predicate: str) -> int:
    for index, step in enumerate(_eval_steps()):
        if predicate in json.dumps(step):
            return index
    msg = f"no eval-job step carries {predicate!r}"
    raise AssertionError(msg)


def _selector() -> dict[str, Any]:
    return _eval_steps()[_step_index('"id": "select"')]


def _eval_step() -> dict[str, Any]:
    return _eval_steps()[_step_index("uv run t3 eval run")]


def _recorder() -> dict[str, Any]:
    return _eval_steps()[_step_index("eval-heal-parked-")]


class TestCredentialWiring:
    def test_the_selector_holds_the_account_pool_and_the_base_token(self) -> None:
        env = _selector()["env"]
        assert env["EVAL_OAUTH_TOKENS"] == "${{ secrets.EVAL_OAUTH_TOKENS }}"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
        assert env["EVAL_SHARD"] == "${{ matrix.shard }}"
        assert "subscription_oauth" in env["EVAL_CREDENTIAL"]

    def test_the_selector_precedes_the_eval_and_tolerates_only_a_park(self) -> None:
        assert _step_index('"id": "select"') < _step_index("uv run t3 eval run")
        assert _selector()["run"] == 'uv run python scripts/eval/select_oauth.py || [ "$?" -eq 75 ]'

    def test_the_heal_lane_never_falls_back_to_the_metered_key(self) -> None:
        assert "EVAL_API_KEY_FALLBACK" not in _text()

    def test_the_eval_step_pins_no_credential_over_the_selection(self) -> None:
        env = _eval_step()["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "T3_AGENT_HARNESS_PROVIDER" not in env

    def test_a_parked_selection_skips_the_eval(self) -> None:
        assert _eval_step()["if"] == "steps.select.outputs.parked != 'true'"

    def test_the_legs_run_two_at_a_time(self) -> None:
        assert _jobs()["eval"]["strategy"]["max-parallel"] == 2


@pytest.fixture
def leg(tmp_path: Path) -> dict[str, str]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    uv = stub_bin / "uv"
    uv.write_text(_STUB_UV, encoding="utf-8")
    uv.chmod(0o755)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    return {
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "REAL_PYTHON": sys.executable,
        "RUNNER_TEMP": str(runner_temp),
        "HEAL_DIR": str(runner_temp / "heal"),
        "GITHUB_SHA": _SHA,
        "HEAD_SHA": _SHA,
        "EVAL_SCENARIOS": "",
        "EVAL_SHARD": "1/8",
        "SUFFIX": "1-8",
        "SELECT_PARKED": "false",
        "T3_CALLS": str(tmp_path / "t3-calls"),
        "T3_RCS": "0",
    }


def _run(body: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, "-eo", "pipefail", "-c", body], env=env, capture_output=True, text=True, check=False, timeout=120
    )


def _attempt(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(cast("str", _eval_step()["with"]["command"]), env)


def _record(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(cast("str", _recorder()["run"]), env)


def _t3_calls(env: dict[str, str]) -> list[str]:
    calls = Path(env["T3_CALLS"])
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def _limit_marker(env: dict[str, str]) -> Path:
    return Path(env["RUNNER_TEMP"]) / "eval-limit-hit"


def _merged(env: dict[str, str]) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(merge_summary_json([env["HEAL_DIR"]], head_sha=_SHA, generated_at="t")))


class TestALegParksOnTheUsageLimit:
    def test_a_limit_exit_parks_the_leg_green_and_marks_it(self, leg: dict[str, str]) -> None:
        leg["T3_RCS"] = "75"

        attempt = _attempt(leg)
        recorded = _record(leg)

        assert attempt.returncode == 0, attempt.stderr
        assert _limit_marker(leg).exists()
        assert recorded.returncode == 0, recorded.stderr
        assert _merged(leg)["parked"] is True

    def test_a_retried_attempt_never_runs_into_the_limit_again(self, leg: dict[str, str]) -> None:
        _limit_marker(leg).write_text("0\n", encoding="utf-8")

        attempt = _attempt(leg)

        assert attempt.returncode == 0, attempt.stderr
        assert _t3_calls(leg) == []

    def test_a_red_eval_fails_the_leg_and_parks_nothing(self, leg: dict[str, str]) -> None:
        leg["T3_RCS"] = "1"

        attempt = _attempt(leg)
        _record(leg)

        assert attempt.returncode == 1
        assert not _limit_marker(leg).exists()
        assert not list(Path(leg["HEAL_DIR"]).glob("eval-heal-parked-*.json"))

    def test_a_red_measured_before_the_limit_stays_red_on_every_attempt(self, leg: dict[str, str]) -> None:
        leg |= {"EVAL_SCENARIOS": "alpha,beta,gamma", "SUFFIX": "subset", "T3_RCS": "1 75"}

        first = _attempt(leg)
        retried = _attempt(leg)

        assert (first.returncode, retried.returncode) == (1, 1)
        assert len(_t3_calls(leg)) == 2
        assert _limit_marker(leg).exists()

    def test_a_red_on_an_earlier_attempt_beats_a_later_limit(self, leg: dict[str, str]) -> None:
        leg["T3_RCS"] = "1 75"
        first = _attempt(leg)
        later = _attempt(leg)
        assert (first.returncode, later.returncode) == (1, 1)
        assert len(_t3_calls(leg)) == 1
        assert not _limit_marker(leg).exists()

    @pytest.mark.parametrize("scenarios", ["", "alpha,beta"], ids=["full-suite", "subset"])
    def test_a_retry_never_turns_a_measured_red_green(self, leg: dict[str, str], scenarios: str) -> None:
        leg |= {"EVAL_SCENARIOS": scenarios, "T3_RCS": "1 0"}
        first = _attempt(leg)
        calls = len(_t3_calls(leg))
        retried = _attempt(leg)
        assert (first.returncode, retried.returncode) == (1, 1)
        assert len(_t3_calls(leg)) == calls

    def test_a_selector_park_records_the_leg_as_parked(self, leg: dict[str, str]) -> None:
        leg["SELECT_PARKED"] = "true"

        recorded = _record(leg)

        assert recorded.returncode == 0, recorded.stderr
        assert _merged(leg) | {"generated_at": "t"} == {
            "generated_at": "t",
            "model": "unknown",
            "head_sha": _SHA,
            "totals": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "incomplete": 0},
            "outcome_counts": {"PASS": 0, "FLAKY": 0, "BEHAVIOR_FAIL": 0, "INFRA_BLOCKED": 0, "UNVERIFIED": 0},
            "scenarios": [],
            "parked": True,
        }


class TestTheGreenProofStep:
    @pytest.mark.parametrize(("proof_rc", "step_rc"), [(0, 0), (75, 0), (1, 1)])
    def test_only_a_park_is_tolerated(self, leg: dict[str, str], proof_rc: int, step_rc: int) -> None:
        step = next(step for step in _jobs()["combine"]["steps"] if "t3 eval green-proof" in str(step.get("run", "")))
        leg |= {"T3_RCS": str(proof_rc), "OUT_DIR": leg["RUNNER_TEMP"], "SHA": _SHA}
        assert _run(step["run"], leg).returncode == step_rc


class TestTheSelectorLine:
    @pytest.mark.parametrize(("selector_rc", "step_rc"), [(0, 0), (75, 0), (1, 1)])
    def test_only_a_park_is_tolerated(self, tmp_path: Path, selector_rc: int, step_rc: int) -> None:
        uv = tmp_path / "uv"
        uv.write_text(f"#!/bin/bash\nexit {selector_rc}\n", encoding="utf-8")
        uv.chmod(0o755)

        result = _run(_selector()["run"], {"PATH": f"{tmp_path}:/usr/bin:/bin"})

        assert result.returncode == step_rc
