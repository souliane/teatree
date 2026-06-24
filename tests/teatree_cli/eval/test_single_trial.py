"""``run_single_trial`` + ``write_single_trial_reports`` — the single-pass ``eval run`` body.

The selective-PR / weekly lanes drive ``run_single_trial`` (the single-trial
sibling of the pass@k / matrix paths). These exercise it end to end against a
stubbed runner so no live model call happens: it runs every spec once, renders,
drops BOTH per-run artifacts (the PRIVATE ``--transcript-html`` transcript and the
SANITIZED ``--summary-md`` dashboard), runs the no-coverage guards, and gates the
result. The artifacts are written from THIS run's in-memory results BEFORE any
guard/gate can exit — so a RED run still drops both, which the failing-path test
pins. ``write_single_trial_reports`` is also exercised directly for its
transcript-html branch.
"""

from pathlib import Path

import pytest

from teatree.cli.eval.app_helpers import write_single_trial_reports
from teatree.cli.eval.single_trial import SingleTrialGates, run_single_trial
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.report import MatcherResult, ScenarioResult

SENTINEL = "SECRET_TRANSCRIPT_LEAK_single_trial"

_NO_GATES = SingleTrialGates(
    persist=False,
    baseline=False,
    gate_regressions=False,
    gate_cost_regression=False,
)


def _spec(name: str, *, lane: str = "clean_room") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
        lane=lane,
    )


def _passing_run(spec_name: str) -> EvalRun:
    return EvalRun(
        spec_name=spec_name,
        tool_calls=(EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),),
        text_blocks=(f"reasoning … {SENTINEL}",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.0,
    )


def _failing_run(spec_name: str) -> EvalRun:
    # No matching tool call ⇒ the positive matcher fails ⇒ the gate reds.
    return EvalRun(
        spec_name=spec_name,
        tool_calls=(),
        text_blocks=(f"reasoning … {SENTINEL}",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.0,
    )


class _StubRunner:
    """A minimal ``EvalRunner`` — returns a canned run per spec, no live model."""

    def __init__(self, run_for) -> None:
        self._run_for = run_for

    def run(self, spec: EvalSpec) -> EvalRun:
        return self._run_for(spec.name)


def _run_with(monkeypatch: pytest.MonkeyPatch, run_for) -> None:
    monkeypatch.setattr(
        "teatree.cli.eval.single_trial.make_runner",
        lambda *_a, **_k: _StubRunner(run_for),
    )


def _call(specs: list[EvalSpec], *, transcript_html: Path | None, summary_md: Path | None) -> None:
    run_single_trial(
        specs,
        backend="transcript",
        max_turns=None,
        transcript_dir=None,
        require_executed=False,
        max_budget_usd=1.0,
        effort=None,
        parallel=1,
        output_format="text",
        grader=None,
        judge=False,
        transcript_html=transcript_html,
        summary_md=summary_md,
        gates=_NO_GATES,
    )


class TestRunSingleTrialArtifacts:
    def test_drops_both_artifacts_for_a_passing_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _run_with(monkeypatch, _passing_run)
        transcript = tmp_path / "transcript.html"
        summary = tmp_path / "summary.md"
        _call([_spec("alpha"), _spec("beta")], transcript_html=transcript, summary_md=summary)
        # The PRIVATE transcript carries the scenario name (and may carry the
        # transcript), the SANITIZED summary carries the verdict table only.
        assert "alpha" in transcript.read_text(encoding="utf-8")
        body = summary.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "beta" in body
        assert "| scenario | lane | verdict | trials |" in body
        assert "2 passed" in body
        # The publish-safe summary never leaks the transcript text.
        assert SENTINEL not in body

    def test_no_artifacts_when_paths_are_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _run_with(monkeypatch, _passing_run)
        sentinel_transcript = tmp_path / "transcript.html"
        sentinel_summary = tmp_path / "summary.md"
        _call([_spec("alpha")], transcript_html=None, summary_md=None)
        assert not sentinel_transcript.exists()
        assert not sentinel_summary.exists()


class TestRunSingleTrialGate:
    def test_failing_run_exits_non_zero_but_artifacts_written_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _run_with(monkeypatch, _failing_run)
        transcript = tmp_path / "transcript.html"
        summary = tmp_path / "summary.md"
        with pytest.raises(SystemExit) as exc:
            _call([_spec("alpha")], transcript_html=transcript, summary_md=summary)
        assert exc.value.code == 1
        # Anti-vacuous: both artifacts must already be on disk even though the
        # gate exited non-zero — the "written before any gate exits" contract.
        assert "alpha" in transcript.read_text(encoding="utf-8")
        summary_body = summary.read_text(encoding="utf-8")
        assert "fail" in summary_body
        assert "1 failed" in summary_body


def _result(name: str, *, passed: bool) -> ScenarioResult:
    matcher = Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x")
    return ScenarioResult(
        spec=_spec(name),
        run=_passing_run(name),
        matcher_results=(MatcherResult(matcher=matcher, passed=passed, message="" if passed else "no match"),),
        skipped=False,
    )


class TestWriteSingleTrialReports:
    def test_writes_only_the_transcript_when_summary_md_is_none(self, tmp_path: Path) -> None:
        transcript = tmp_path / "transcript.html"
        summary = tmp_path / "summary.md"
        write_single_trial_reports([_result("alpha", passed=True)], transcript_html=transcript, summary_md=None)
        # The transcript-html branch wrote a self-contained HTML report …
        html = transcript.read_text(encoding="utf-8")
        assert "<!doctype html>" in html
        assert "alpha" in html
        # … and the summary branch was a no-op.
        assert not summary.exists()

    def test_writes_both_when_both_paths_given(self, tmp_path: Path) -> None:
        transcript = tmp_path / "transcript.html"
        summary = tmp_path / "summary.md"
        write_single_trial_reports([_result("alpha", passed=True)], transcript_html=transcript, summary_md=summary)
        assert "<!doctype html>" in transcript.read_text(encoding="utf-8")
        assert "| scenario | lane | verdict | trials |" in summary.read_text(encoding="utf-8")

    def test_no_op_when_both_paths_none(self, tmp_path: Path) -> None:
        transcript = tmp_path / "transcript.html"
        summary = tmp_path / "summary.md"
        write_single_trial_reports([_result("alpha", passed=True)], transcript_html=None, summary_md=None)
        assert not transcript.exists()
        assert not summary.exists()
