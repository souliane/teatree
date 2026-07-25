"""A red behavioral-eval leg must be diagnosable from the run that already spent.

Each metered leg costs ~2% of the subscription's weekly usage window, so a leg that
fails while emitting only the retry action's ``Child_process exited with error code
1`` makes every diagnostic re-run pure waste. These tests pin the observability
contract in ``.github/workflows/eval.yml``: the run's output is captured to a durable
file, every diagnostic artifact uploads unconditionally under a per-leg-unique name,
and a failing leg re-prints its tail + summary into the job log while still
propagating the real exit code.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"

_SUFFIX_EXPR = "steps.artifact.outputs.suffix"
_UPLOAD_ACTION = "actions/upload-artifact"
_RETRY_ACTION = "nick-fields/retry"


def _eval_steps() -> list[dict[str, Any]]:
    jobs = cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8"))["jobs"])
    return cast("list[dict[str, Any]]", jobs["eval"]["steps"])


def _upload_steps() -> list[dict[str, Any]]:
    steps = [step for step in _eval_steps() if step.get("uses", "").startswith(_UPLOAD_ACTION)]
    assert steps, "the eval job must upload diagnostic artifacts."
    return steps


def _eval_command() -> str:
    for step in _eval_steps():
        if step.get("uses", "").startswith(_RETRY_ACTION) and "t3 eval run" in step.get("with", {}).get("command", ""):
            return cast("str", step["with"]["command"])
    msg = "No retry-wrapped `t3 eval run` step in the eval job."
    raise AssertionError(msg)


def _upload_step_named(name_fragment: str) -> dict[str, Any]:
    for step in _upload_steps():
        if name_fragment in step["with"]["name"]:
            return step
    msg = f"No upload-artifact step publishing a {name_fragment!r} artifact."
    raise AssertionError(msg)


class TestDiagnosticArtifactsSurviveAFailingLeg:
    def test_every_artifact_upload_is_unconditional(self) -> None:
        # A success-only (or absent) `if:` drops the artifact on exactly the runs that
        # need it — the whole point of uploading a summary is to explain a failure.
        for step in _upload_steps():
            assert step.get("if") == "always()", (
                f"Upload step {step.get('name')!r} must be `if: always()` so a RED leg still "
                f"publishes its diagnostics; got {step.get('if')!r}."
            )

    def test_artifact_names_are_unique_per_matrix_leg(self) -> None:
        # The fan-out runs many legs in parallel; a shared artifact name collides and
        # silently keeps only one leg's diagnostics.
        for step in _upload_steps():
            assert _SUFFIX_EXPR in step["with"]["name"], (
                f"Artifact name {step['with']['name']!r} must carry the per-leg "
                f"lane/shard/effort suffix so parallel legs cannot collide."
            )

    def test_raw_run_log_is_uploaded(self) -> None:
        # The transcript and the summary are rendered at END of run, so a leg that
        # dies mid-run writes neither. The teed log exists from the first byte.
        step = _upload_step_named("eval-run-log")
        assert "eval-run-" in step["with"]["path"], "the raw-log artifact must publish the teed run log."


class TestFailureOutputReachesTheJobLog:
    def test_run_output_is_captured_to_a_durable_file(self) -> None:
        command = _eval_command()
        assert "tee -a" in command, (
            "The eval run's combined output must be teed to a file: streaming alone leaves "
            "nothing to upload and loses everything killed by the step timeout."
        )
        assert "2>&1" in command, "stderr must be merged into the captured stream (tracebacks land on stderr)."
        assert 'LOG="$RUNNER_TEMP/eval-run-$EVAL_SUFFIX.log"' in command, (
            "the captured log must live in $RUNNER_TEMP under the per-leg suffix so it can be uploaded."
        )

    def test_failure_prints_the_captured_tail_and_the_summary(self) -> None:
        command = _eval_command()
        assert "tail -n" in command, "a failing leg must print the tail of the captured output into the job log."
        assert 'cat "$SUMMARY"' in command, "a failing leg must print the summary markdown when one was rendered."

    def test_the_real_exit_code_is_preserved(self) -> None:
        command = _eval_command()
        assert "set -o pipefail" in command, (
            "`tee` would otherwise mask a non-zero eval exit code and turn a red leg green."
        )
        assert 'exit "$rc"' in command, (
            "The failure handler adds visibility, never swallows the error — the leg must still fail."
        )

    def test_each_attempt_reports_its_own_failure(self) -> None:
        # Printing INSIDE the retried command is what makes attempt 1 visible; a
        # post-step handler would only ever see the final attempt.
        command = _eval_command()
        marker = command.index("tail -n")
        assert command.index("t3 eval run") < marker, (
            "the tail print must follow the eval invocation inside the retried command, "
            "so every attempt surfaces its own failure output."
        )
