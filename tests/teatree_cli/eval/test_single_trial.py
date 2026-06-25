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
from unittest.mock import patch

import pytest

from teatree.cli.eval.app_helpers import write_single_trial_reports
from teatree.cli.eval.single_trial import EscalationConfig, SingleTrialGates, make_escalation_runner, run_single_trial
from teatree.eval.api_runner import ApiInProcessRunner
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.report import MatcherResult, ScenarioResult
from teatree.llm.credentials import AnthropicApiKeyCredential

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


def _call(
    specs: list[EvalSpec],
    *,
    transcript_html: Path | None,
    summary_md: Path | None,
    escalation: EscalationConfig | None = None,
) -> None:
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
        escalation=escalation,
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


class TestMakeEscalationRunner:
    def test_builds_a_metered_api_runner_carrying_the_lane_effort(self) -> None:
        # Escalation always RUNS the model fresh, so it is the metered api backend
        # regardless of the initial backend, and it carries the lane effort.
        with patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test"):
            runner = make_escalation_runner(max_budget_usd=2.0, effort="high")
        assert isinstance(runner, ApiInProcessRunner)


class _EscalationStubRunner:
    """A metered escalation runner — maps a scenario name to a queue of pass/fail verdicts."""

    def __init__(self, scripts: dict[str, list[bool]]) -> None:
        self._iters = {name: iter(verdicts) for name, verdicts in scripts.items()}
        self.calls: dict[str, int] = {}

    def run(self, spec: EvalSpec) -> EvalRun:
        self.calls[spec.name] = self.calls.get(spec.name, 0) + 1
        passed = next(self._iters[spec.name])
        # A metered trial bills a non-zero cost so the unmetered-$0 guard stays green.
        return _passing_run(spec.name) if passed else _failing_run(spec.name)


def _arm_escalation_runner(monkeypatch: pytest.MonkeyPatch, scripts: dict[str, list[bool]]) -> _EscalationStubRunner:
    runner = _EscalationStubRunner(scripts)
    monkeypatch.setattr(
        "teatree.cli.eval.single_trial.make_escalation_runner",
        lambda **_k: runner,
    )
    return runner


class TestRunSingleTrialEscalation:
    def test_flaky_escalation_stays_green(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Trial 1 fails; the escalation has a passing trial → flaky, NOT a hard red.
        _run_with(monkeypatch, _failing_run)
        runner = _arm_escalation_runner(monkeypatch, {"alpha": [True, False, False]})
        summary = tmp_path / "summary.md"
        # No SystemExit: a flaky-but-passing scenario does not red the lane.
        _call(
            [_spec("alpha")],
            transcript_html=None,
            summary_md=summary,
            escalation=EscalationConfig(escalate_trials=3),
        )
        assert runner.calls == {"alpha": 3}
        body = summary.read_text(encoding="utf-8")
        assert "flaky" in body.lower()

    def test_confirmed_escalation_reds_the_lane(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Trial 1 fails; every escalation trial fails too → confirmed, hard red.
        _run_with(monkeypatch, _failing_run)
        runner = _arm_escalation_runner(monkeypatch, {"alpha": [False, False, False]})
        summary = tmp_path / "summary.md"
        with pytest.raises(SystemExit) as exc:
            _call(
                [_spec("alpha")],
                transcript_html=None,
                summary_md=summary,
                escalation=EscalationConfig(escalate_trials=3),
            )
        assert exc.value.code == 1
        assert runner.calls == {"alpha": 3}
        assert "confirmed" in summary.read_text(encoding="utf-8").lower()

    def test_passing_trial_one_never_escalates(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # An all-green trial-1 run never spends escalation trials — the cheap path.
        _run_with(monkeypatch, _passing_run)
        runner = _arm_escalation_runner(monkeypatch, {})
        _call(
            [_spec("alpha")],
            transcript_html=None,
            summary_md=tmp_path / "summary.md",
            escalation=EscalationConfig(escalate_trials=3),
        )
        assert runner.calls == {}

    def test_flaky_escalation_without_a_summary_path_still_stays_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The summary_md=None branch: a flaky escalation must not red and must not
        # try to write a missing summary file.
        _run_with(monkeypatch, _failing_run)
        _arm_escalation_runner(monkeypatch, {"alpha": [True, False, False]})
        _call([_spec("alpha")], transcript_html=None, summary_md=None, escalation=EscalationConfig(escalate_trials=3))

    def test_no_escalation_config_reds_immediately_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Without escalation, a trial-1 failure reds the lane with no re-run — the
        # legacy single-trial behaviour is unchanged when escalation is off.
        _run_with(monkeypatch, _failing_run)
        with pytest.raises(SystemExit) as exc:
            _call([_spec("alpha")], transcript_html=None, summary_md=tmp_path / "summary.md", escalation=None)
        assert exc.value.code == 1


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
