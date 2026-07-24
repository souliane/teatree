"""The always-conclusive `eval-gate` job makes the selective PR eval a requirable check.

The selective-PR eval's `eval` job is conditionally skipped when a PR changes no
scenario file. A conditionally-skipped job is a fragile required status check —
GitHub leaves a skipped required context pending and wedges every no-change PR. The
`eval-gate` job closes that: it `needs: [detect, eval]` with `if: always()`, so it
ALWAYS runs and produces exactly ONE conclusive success/failure check that branch
protection can pin as a stable required context.

These tests pin BOTH halves of the gate: the static contract (the job exists in the
host workflow AND the reusable one, always runs, and depends on both upstream jobs)
and the behavioral truth table (the gate's own shell, executed under GitHub's
`bash -eo pipefail` semantics against every `needs.*.result` combination). The
behavioral test executes the REAL shipped `run:` body, so it goes red if the gate
logic drifts — not a re-implementation of it.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_BASH = shutil.which("bash") or "/bin/bash"

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_HOST = _WORKFLOWS / "eval-pr.yml"
_REUSABLE = _WORKFLOWS / "eval-pr-reusable.yml"
_GATE_WORKFLOWS = [_HOST, _REUSABLE]


def _jobs(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"])


def _gate_run_body(path: Path) -> str:
    for step in cast("list[dict[str, Any]]", _jobs(path)["eval-gate"]["steps"]):
        if "run" in step:
            return cast("str", step["run"])
    msg = f"{path.name}: the eval-gate job has no `run` step."
    raise AssertionError(msg)


def _run_gate(path: Path, *, detect: str, eval_result: str, changed: str) -> subprocess.CompletedProcess[str]:
    # Execute the gate's real shell body under GitHub's default `run:` shell
    # semantics (bash -eo pipefail), so the test exercises the shipped logic, not
    # a copy of it. `needs.*.result` and the detect output are supplied as env.
    script = "set -eo pipefail\n" + _gate_run_body(path)
    return subprocess.run(
        [_BASH, "-c", script],
        env={"DETECT_RESULT": detect, "EVAL_RESULT": eval_result, "CHANGED": changed, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


class TestGateContract:
    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_gate_always_runs_and_needs_both_jobs(self, path: Path) -> None:
        gate = cast("dict[str, Any]", _jobs(path)["eval-gate"])
        assert gate["if"] == "always()", (
            f"{path.name}: eval-gate must run with `if: always()` so it produces a conclusive "
            "check even when the eval job is skipped — a skipped required check wedges merges."
        )
        assert gate["needs"] == ["detect", "eval"], (
            f"{path.name}: eval-gate must depend on both `detect` and `eval` to read their results."
        )

    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_gate_reads_the_upstream_results_not_the_metered_schedule(self, path: Path) -> None:
        # The gate must conclude from THIS workflow's two jobs only — never the
        # weekly/nightly metered runs, whose depleting subscription window would
        # otherwise wedge every merge.
        gate = cast("dict[str, Any]", _jobs(path)["eval-gate"])
        env = cast("dict[str, str]", gate["steps"][0]["env"])
        assert env["DETECT_RESULT"] == "${{ needs.detect.result }}"
        assert env["EVAL_RESULT"] == "${{ needs.eval.result }}"


class TestGateTruthTable:
    """The four cases the task defines, executed against the shipped gate shell."""

    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_no_scenario_changed_is_green(self, path: Path) -> None:
        # detect succeeds, eval is skipped (no scenario changed) → PASS. The common case.
        result = _run_gate(path, detect="success", eval_result="skipped", changed="false")
        assert result.returncode == 0, result.stderr
        assert "PASS — no scenario changed" in result.stdout

    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_changed_and_passing_is_green(self, path: Path) -> None:
        # detect succeeds, eval succeeds (changed scenarios passed) → PASS.
        result = _run_gate(path, detect="success", eval_result="success", changed="true")
        assert result.returncode == 0, result.stderr
        assert "PASS — the PR's changed scenarios ran and passed" in result.stdout

    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_changed_and_failing_is_red(self, path: Path) -> None:
        # detect succeeds, eval fails (a changed scenario failed) → FAIL.
        result = _run_gate(path, detect="success", eval_result="failure", changed="true")
        assert result.returncode == 1
        assert "FAIL — the selective eval did not pass" in result.stdout

    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_vacuous_zero_executed_surfaces_as_red(self, path: Path) -> None:
        # The ZERO-IS-RED guard `exit 1`s INSIDE the eval job, so a supposed-to-run
        # run that executed zero scenarios reaches the gate as eval=failure — the
        # same red the gate emits for a real scenario failure. This asserts the
        # vacuous case flows into the gate rather than being swallowed as green.
        result = _run_gate(path, detect="success", eval_result="failure", changed="true")
        assert result.returncode == 1

    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_detection_failure_is_red_even_when_eval_skipped(self, path: Path) -> None:
        # A bad ref / resolver error fails `detect`, which skips `eval`. The gate
        # must red on the detect failure rather than green on the skipped eval.
        result = _run_gate(path, detect="failure", eval_result="skipped", changed="")
        assert result.returncode == 1
        assert "FAIL — scenario detection did not succeed" in result.stdout

    @pytest.mark.parametrize("path", _GATE_WORKFLOWS, ids=lambda p: p.name)
    def test_cancelled_job_is_red(self, path: Path) -> None:
        # A cancelled eval (timeout / superseded) is not a pass — fail conservatively.
        result = _run_gate(path, detect="success", eval_result="cancelled", changed="true")
        assert result.returncode == 1
