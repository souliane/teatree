"""``t3 eval run --trials k --transcript-html <path>`` — the per-trial artifact.

The metered CI run is ``--no-persist`` inside an ephemeral, read-only-mounted
container, so nothing reaches the host run-history ledger. ``--transcript-html``
makes the run emit its OWN per-trial transcript report (verdicts + each trial's
reasoning + tool calls), written from this run's in-memory results — NO suite
re-run, NO ledger read — to a writable path. These tests pin that the lane:
writes the file, includes the per-trial transcript, does NOT re-execute the
runner an extra time for rendering, and still writes the artifact when the run
is about to exit non-zero (a red lane is exactly what needs diagnosing).
"""

from pathlib import Path

import pytest

from teatree.cli.eval.multi_trial import run_pass_at_k_lane
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        judge=None,
    )


class _CountingRunner:
    """A clean runner that counts how many times it actually executed a scenario."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, spec: EvalSpec) -> EvalRun:
        self.calls += 1
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(EvalToolCall(name="Bash", input={"command": f"echo {spec.name}"}, turn=1),),
            text_blocks=(f"reasoning for {spec.name}",),
            terminal_reason="end_turn",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


@pytest.fixture
def counting_runner(monkeypatch: pytest.MonkeyPatch) -> _CountingRunner:
    runner = _CountingRunner()
    monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: runner)
    return runner


class TestTranscriptHtmlArtifact:
    def test_writes_the_report_to_the_given_writable_path(
        self, counting_runner: _CountingRunner, tmp_path: Path
    ) -> None:
        out = tmp_path / "eval-transcripts.html"
        run_pass_at_k_lane(
            [_spec("alpha"), _spec("beta")],
            max_turns=None,
            trials=3,
            require="any",
            output_format="text",
            persist=False,
            transcript_html=out,
        )
        assert out.is_file()
        body = out.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "beta" in body

    def test_includes_each_trials_transcript(self, counting_runner: _CountingRunner, tmp_path: Path) -> None:
        out = tmp_path / "report.html"
        run_pass_at_k_lane(
            [_spec("alpha")],
            max_turns=None,
            trials=3,
            require="any",
            output_format="text",
            persist=False,
            transcript_html=out,
        )
        body = out.read_text(encoding="utf-8")
        # The agent's reasoning AND tool call are present — the per-trial evidence.
        assert "reasoning for alpha" in body
        assert "echo alpha" in body

    def test_does_not_re_execute_the_runner_to_render(self, counting_runner: _CountingRunner, tmp_path: Path) -> None:
        # The whole bug: the old render step re-ran the suite. The artifact must be
        # rendered from THIS run's results — so the runner is invoked exactly
        # trials x scenarios times, never an extra pass for the report.
        out = tmp_path / "report.html"
        run_pass_at_k_lane(
            [_spec("alpha"), _spec("beta")],
            max_turns=None,
            trials=3,
            require="any",
            output_format="text",
            persist=False,
            transcript_html=out,
        )
        # 2 scenarios x 3 trials = 6 runs, and not one more for rendering.
        assert counting_runner.calls == 6

    def test_no_file_written_when_path_is_none(self, counting_runner: _CountingRunner, tmp_path: Path) -> None:
        report = tmp_path / "report.html"
        run_pass_at_k_lane(
            [_spec("alpha")],
            max_turns=None,
            trials=2,
            require="any",
            output_format="text",
            persist=False,
            transcript_html=None,
        )
        assert not report.exists()


class _FailingRunner:
    """A runner whose scenario FAILS its matcher — the lane will want to exit 1."""

    def run(self, spec: EvalSpec) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=("I did the wrong thing",),
            terminal_reason="success",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


class TestArtifactWrittenEvenOnRedRun:
    def test_artifact_is_written_before_the_non_zero_exit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A red lane is exactly what a maintainer needs the transcript for, so the
        # artifact must land even though the lane is about to exit non-zero.
        monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _FailingRunner())
        spec = EvalSpec(
            name="fails",
            scenario="s",
            agent_path="skills/code/SKILL.md",
            prompt="do",
            matchers=(
                # A positive matcher that the runner's empty tool_calls cannot satisfy → FAIL.
                Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="never-emitted"),
            ),
            source_path=Path("/tmp/spec.yaml"),
            judge=None,
        )
        out = tmp_path / "report.html"
        failed = run_pass_at_k_lane(
            [spec],
            max_turns=None,
            trials=2,
            require="any",
            output_format="text",
            persist=False,
            model_override="claude-sonnet-4-6",  # suppress the sys.exit so we can assert the file
            transcript_html=out,
        )
        assert failed is True
        assert out.is_file()
        assert "fails" in out.read_text(encoding="utf-8")
