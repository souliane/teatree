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

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.cli.eval.app_helpers import write_single_trial_reports
from teatree.cli.eval.single_trial import EscalationConfig, SingleTrialGates, make_escalation_runner, run_single_trial
from teatree.eval.anthropic_api_runner import AnthropicApiKeyMissingError, AnthropicApiRunner
from teatree.eval.api_runner import ApiInProcessRunner, ApiRunnerParams
from teatree.eval.backends import (
    ANTHROPIC_API_BACKEND,
    API_BACKEND,
    PYDANTIC_AI_BACKEND,
    TRANSCRIPT_BACKEND,
    EvalRunner,
)
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.report import MatcherResult, ScenarioResult
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential, CredentialError

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


@dataclasses.dataclass(frozen=True)
class _Lane:
    """The two lane knobs whose escalation behaviour these tests vary."""

    backend: str = TRANSCRIPT_BACKEND
    require_executed: bool = False


_DEFAULT_LANE = _Lane()


def _call(
    specs: list[EvalSpec],
    *,
    transcript_html: Path | None,
    summary_md: Path | None,
    escalation: EscalationConfig | None = None,
    lane: _Lane = _DEFAULT_LANE,
) -> None:
    run_single_trial(
        specs,
        backend=lane.backend,
        max_turns=None,
        transcript_dir=None,
        require_executed=lane.require_executed,
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


class _RecordingRunnerFactory:
    """Stands in for ``make_runner``, recording the backend + params each build asked for."""

    def __init__(self, run_for=_failing_run) -> None:
        self.calls: list[tuple[str, ApiRunnerParams]] = []
        self._run_for = run_for

    def __call__(self, backend: str, params: ApiRunnerParams | None = None, **_kwargs) -> EvalRunner:
        self.calls.append((backend, params or ApiRunnerParams()))
        return _StubRunner(self._run_for)


class TestMakeEscalationRunner(TestCase):
    def test_the_cli_free_backend_escalates_on_itself_never_on_the_claude_cli(self) -> None:
        # The anthropic_api lane exists to run WITHOUT a `claude` child; escalating it
        # onto the CLI-backed api runner re-introduces the very binary the lane avoids.
        runner = make_escalation_runner(
            backend=ANTHROPIC_API_BACKEND, max_budget_usd=2.0, effort="high", require_executed=False
        )
        assert isinstance(runner, AnthropicApiRunner)
        assert not isinstance(runner, ApiInProcessRunner)

    def test_the_transcript_backend_is_the_only_one_that_falls_back_to_api(self) -> None:
        # A transcript replays a recorded run, so it genuinely cannot produce a fresh
        # trial — the ONE case where escalation must switch transports. It carries the
        # lane effort and resolves the default eval credential (subscription OAuth).
        with patch.object(AnthropicSubscriptionCredential, "export", return_value="oauth-test"):
            runner = make_escalation_runner(
                backend=TRANSCRIPT_BACKEND, max_budget_usd=2.0, effort="high", require_executed=False
            )
        assert isinstance(runner, ApiInProcessRunner)

    def test_require_executed_makes_an_unrunnable_escalation_fail_loud(self) -> None:
        # An escalation trial that cannot execute must RAISE, not skip: a silent skip is
        # the "0/3 escalation trials" symptom that hid a genuine trial-1 failure.
        runner = make_escalation_runner(
            backend=ANTHROPIC_API_BACKEND, max_budget_usd=2.0, effort=None, require_executed=True
        )
        with (
            patch.object(AnthropicApiKeyCredential, "resolve", side_effect=CredentialError("no key")),
            pytest.raises(AnthropicApiKeyMissingError),
        ):
            runner.run(_spec("alpha"))

    def test_without_require_executed_the_same_escalation_still_skips(self) -> None:
        # The contrast that pins the FORWARDING: same unrunnable transport, flag off →
        # the pre-existing skip. So the loud failure above comes from the forwarded flag.
        runner = make_escalation_runner(
            backend=ANTHROPIC_API_BACKEND, max_budget_usd=2.0, effort=None, require_executed=False
        )
        with patch.object(AnthropicApiKeyCredential, "resolve", side_effect=CredentialError("no key")):
            run = runner.run(_spec("alpha"))
        assert "skipped" in run.terminal_reason


class TestEscalationBackendSelectionRule:
    @pytest.mark.parametrize(
        ("backend", "escalation_backend"),
        [
            (API_BACKEND, API_BACKEND),
            (ANTHROPIC_API_BACKEND, ANTHROPIC_API_BACKEND),
            (PYDANTIC_AI_BACKEND, PYDANTIC_AI_BACKEND),
            (TRANSCRIPT_BACKEND, API_BACKEND),
        ],
    )
    def test_mirrors_every_fresh_backend_and_rewrites_only_transcript(
        self, monkeypatch: pytest.MonkeyPatch, backend: str, escalation_backend: str
    ) -> None:
        factory = _RecordingRunnerFactory()
        monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", factory)
        make_escalation_runner(backend=backend, max_budget_usd=2.0, effort=None, require_executed=False)
        assert [called for called, _ in factory.calls] == [escalation_backend]

    def test_the_escalation_inherits_the_lanes_backend_and_require_executed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # End to end through run_single_trial: a failing anthropic_api trial-1 escalates
        # on anthropic_api with the lane's --require-executed still armed.
        factory = _RecordingRunnerFactory()
        monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", factory)
        with pytest.raises(SystemExit):
            _call(
                [_spec("alpha")],
                transcript_html=None,
                summary_md=tmp_path / "summary.md",
                escalation=EscalationConfig(escalate_trials=3),
                lane=_Lane(backend=ANTHROPIC_API_BACKEND, require_executed=True),
            )
        escalation_backend, escalation_params = factory.calls[-1]
        assert escalation_backend == ANTHROPIC_API_BACKEND
        assert escalation_params.require_executed is True


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
