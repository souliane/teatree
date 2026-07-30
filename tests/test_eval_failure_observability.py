"""A red behavioral-eval leg must be diagnosable from the run that already spent.

Each metered leg costs ~2% of the subscription's weekly usage window (and real
per-token billing on the benchmark's api-key lane), so a leg that fails while
emitting only the retry action's ``Child_process exited with error code 1`` makes
every diagnostic re-run pure waste. These tests pin the observability contract in
all three metered workflows — ``.github/workflows/eval.yml`` (the weekly cron),
``.github/workflows/eval-weekly-reusable.yml`` (the manual benchmark) and
``.github/workflows/eval-pr-reusable.yml`` (the per-PR selective lane): the run's
output is captured to a durable file, every diagnostic artifact uploads
unconditionally under a name the run actually writes, and a failing leg re-prints
its tail into the job log while still propagating the real exit code.
"""

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"
_GH_BENCHMARK = _REPO_ROOT / ".github" / "workflows" / "eval-weekly-reusable.yml"
_GH_PR = _REPO_ROOT / ".github" / "workflows" / "eval-pr-reusable.yml"

# All three spend real credit per run, so all three owe the same post-mortem.
_METERED_WORKFLOWS = (_GH_EVAL, _GH_BENCHMARK, _GH_PR)

# Only the fan-out lanes can collide on an artifact name; the PR lane's eval job is
# a single job with no matrix, so a per-leg suffix would be noise there.
_MATRIX_WORKFLOWS = (_GH_EVAL, _GH_BENCHMARK)

_SUFFIX_EXPR = "steps.artifact.outputs.suffix"
_UPLOAD_ACTION = "actions/upload-artifact"
_RETRY_ACTION = "nick-fields/retry"
_LOG_ASSIGNMENT = re.compile(r'LOG="\$RUNNER_TEMP/eval-run-[^"]*\.log"')
_RUNNER_TEMP_REF = re.compile(r"\$RUNNER_TEMP/(?P<name>[A-Za-z0-9_.$-]+)")
#: A shell variable, a GitHub expression and a glob are all "the varying part" here;
#: collapsing them to one placeholder compares the SHAPE of the two filenames without
#: needing to know each variable's runtime value.
_VARYING_PART = re.compile(r"\$\{\{[^}]*\}\}|\$[A-Za-z_][A-Za-z0-9_]*|\*")


def _artifact_shape(path: str) -> str:
    bare = path.replace("${{ runner.temp }}/", "").replace("$RUNNER_TEMP/", "")
    return _VARYING_PART.sub("<var>", bare)


def _eval_steps(workflow: Path) -> list[dict[str, Any]]:
    jobs = cast("dict[str, Any]", yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"])
    return cast("list[dict[str, Any]]", jobs["eval"]["steps"])


def _upload_steps(workflow: Path) -> list[dict[str, Any]]:
    steps = [step for step in _eval_steps(workflow) if step.get("uses", "").startswith(_UPLOAD_ACTION)]
    assert steps, f"{workflow.name}: the eval job must upload diagnostic artifacts."
    return steps


def _eval_command(workflow: Path) -> str:
    for step in _eval_steps(workflow):
        if step.get("uses", "").startswith(_RETRY_ACTION) and "t3 eval run" in step.get("with", {}).get("command", ""):
            return cast("str", step["with"]["command"])
    msg = f"{workflow.name}: no retry-wrapped `t3 eval run` step in the eval job."
    raise AssertionError(msg)


def _upload_step_named(workflow: Path, name_fragment: str) -> dict[str, Any]:
    for step in _upload_steps(workflow):
        if name_fragment in step["with"]["name"]:
            return step
    msg = f"{workflow.name}: no upload-artifact step publishing a {name_fragment!r} artifact."
    raise AssertionError(msg)


@pytest.mark.parametrize("workflow", _METERED_WORKFLOWS, ids=lambda path: path.name)
class TestDiagnosticArtifactsSurviveAFailingLeg:
    def test_every_artifact_upload_is_unconditional(self, workflow: Path) -> None:
        # A success-only (or absent) `if:` drops the artifact on exactly the runs that
        # need it — the whole point of uploading a summary is to explain a failure.
        for step in _upload_steps(workflow):
            assert step.get("if") == "always()", (
                f"{workflow.name}: upload step {step.get('name')!r} must be `if: always()` so a RED "
                f"leg still publishes its diagnostics; got {step.get('if')!r}."
            )

    def test_every_upload_publishes_a_path_the_run_actually_writes(self, workflow: Path) -> None:
        # An `if: always()` upload of a filename nothing writes is worse than no upload:
        # `if-no-files-found: warn` makes it fail silently, so the artifact list reads as
        # complete while the report that would explain the failure was never published.
        command = _eval_command(workflow)
        written = {_artifact_shape(match.group("name")) for match in _RUNNER_TEMP_REF.finditer(command)}
        for step in _upload_steps(workflow):
            published = _artifact_shape(step["with"]["path"])
            assert published in written, (
                f"{workflow.name}: upload step {step.get('name')!r} publishes {published!r}, which the eval "
                f"command never writes (it renders {sorted(written)})."
            )

    def test_raw_run_log_is_uploaded(self, workflow: Path) -> None:
        # Every other artifact (transcript, summary, benchmark matrix) is rendered at
        # END of run, so a leg that dies mid-run writes none of them. The teed log
        # exists from the first byte.
        step = _upload_step_named(workflow, "eval-run-log")
        assert "eval-run-" in step["with"]["path"], (
            f"{workflow.name}: the raw-log artifact must publish the teed run log."
        )


@pytest.mark.parametrize("workflow", _METERED_WORKFLOWS, ids=lambda path: path.name)
class TestFailureOutputReachesTheJobLog:
    def test_run_output_is_captured_to_a_durable_file(self, workflow: Path) -> None:
        command = _eval_command(workflow)
        assert "tee -a" in command, (
            f"{workflow.name}: the eval run's combined output must be teed to a file — streaming "
            f"alone leaves nothing to upload and loses everything killed by the step timeout."
        )
        assert "2>&1" in command, (
            f"{workflow.name}: stderr must be merged into the captured stream (tracebacks land on stderr)."
        )
        assert _LOG_ASSIGNMENT.search(command), (
            f"{workflow.name}: the captured log must live at $RUNNER_TEMP/eval-run-*.log so it can be uploaded."
        )

    def test_failure_prints_the_captured_tail(self, workflow: Path) -> None:
        command = _eval_command(workflow)
        assert "tail -n" in command, (
            f"{workflow.name}: a failing leg must print the tail of the captured output into the job log."
        )

    def test_the_real_exit_code_is_preserved(self, workflow: Path) -> None:
        command = _eval_command(workflow)
        assert "set -o pipefail" in command, (
            f"{workflow.name}: `tee` would otherwise mask a non-zero eval exit code and turn a red leg green."
        )
        assert 'exit "$rc"' in command, (
            f"{workflow.name}: the failure handler adds visibility, never swallows the error — the leg must still fail."
        )

    def test_each_attempt_reports_its_own_failure(self, workflow: Path) -> None:
        # Printing INSIDE the retried command is what makes attempt 1 visible; a
        # post-step handler would only ever see the final attempt.
        command = _eval_command(workflow)
        marker = command.index("tail -n")
        assert command.index("t3 eval run") < marker, (
            f"{workflow.name}: the tail print must follow the eval invocation inside the retried "
            f"command, so every attempt surfaces its own failure output."
        )


@pytest.mark.parametrize("workflow", _MATRIX_WORKFLOWS, ids=lambda path: path.name)
class TestParallelLegsCannotCollideOnAnArtifactName:
    def test_artifact_names_are_unique_per_matrix_leg(self, workflow: Path) -> None:
        # The fan-out runs many legs in parallel; a shared artifact name collides and
        # silently keeps only one leg's diagnostics.
        for step in _upload_steps(workflow):
            assert _SUFFIX_EXPR in step["with"]["name"], (
                f"{workflow.name}: artifact name {step['with']['name']!r} must carry the per-leg "
                f"lane/shard/effort suffix so parallel legs cannot collide."
            )


class TestTheEndOfRunArtifactIsAccountedForOnFailure:
    """Each lane's end-of-run artifact is either shown or its absence stated.

    A maintainer must never be left guessing whether the artifact is missing from
    the run or merely missing from the log, so each failure block branches on the
    file rather than staying silent about it.
    """

    def test_weekly_cron_prints_the_summary_markdown(self) -> None:
        command = _eval_command(_GH_EVAL)
        assert 'cat "$SUMMARY"' in command, "a failing leg must print the summary markdown when one was rendered."

    def test_benchmark_reports_whether_the_matrix_dashboard_rendered(self) -> None:
        # The benchmark's end-of-run artifact is an HTML dashboard — useless catted
        # into a job log, so the block points at the uploaded artifact instead. Its
        # text twin (the matrix table) is already inside the teed tail.
        command = _eval_command(_GH_BENCHMARK)
        assert '[ -s "$MATRIX_HTML" ]' in command, (
            "a failing benchmark leg must say whether the matrix dashboard rendered — "
            "its absence is what tells a maintainer the run died mid-matrix."
        )
