"""An ``interactive``-surface scenario is graded and reported, but never gates a lane.

This is the property that let the ``claude-agent-sdk`` Dependabot quarantine go
(souliane/teatree#3855): a bundled-CLI rendering change reds only the advisory
interactive scenarios, so no gating verdict depends on the tool-call wire shape.

The exemption has to hold at EVERY verdict point or the quarantine still defends
something. Covered here: the full-suite AI lane (``_ai_lane_result``), and the two
exits of the DEFAULT ``trials=1``/no-``--models`` lane every metered CI leg drives —
``finalize_single_run`` and the ``--escalate-on-fail`` escalation. The pass@k and
model-matrix verdicts are covered by their own lane tests.
"""

from pathlib import Path

import pytest

from teatree.cli.eval.all import _ai_lane_result
from teatree.cli.eval.escalate import EscalationConfig, escalate_failures
from teatree.cli.eval.run_modes import finalize_single_run
from teatree.cli.eval.single_trial import SingleTrialGates, run_single_trial
from teatree.eval.models import HEADLESS_SURFACE, INTERACTIVE_SURFACE, EvalRun, EvalSpec, Matcher
from teatree.eval.report import ScenarioResult
from teatree.eval.surface import INTERACTIVE_QUESTION_TOOL


def _spec(name: str, *, surface: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=(),
        source_path=Path("synthetic.yaml"),
        surface=surface,
    )


def _result(name: str, *, surface: str, passed: bool) -> ScenarioResult:
    spec = _spec(name, surface=surface)
    # is_error is the cheapest way to force a FAIL verdict with no matcher machinery.
    run = EvalRun(
        spec_name=name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="success",
        is_error=not passed,
        raw_stdout="",
        raw_stderr="",
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=False)


def test_a_failing_interactive_scenario_does_not_fail_the_lane() -> None:
    lane = _ai_lane_result([_result("chip", surface=INTERACTIVE_SURFACE, passed=False)], backend="api", graded=True)
    assert lane.passed


def test_a_failing_interactive_scenario_is_still_reported() -> None:
    # Advisory is not invisible: the count must stay in the detail line, or a real
    # interactive regression becomes silent rather than merely non-blocking.
    lane = _ai_lane_result([_result("chip", surface=INTERACTIVE_SURFACE, passed=False)], backend="api", graded=True)
    assert "1 advisory failed" in lane.detail


def test_a_failing_headless_scenario_still_fails_the_lane() -> None:
    lane = _ai_lane_result([_result("slack", surface=HEADLESS_SURFACE, passed=False)], backend="api", graded=True)
    assert not lane.passed


def test_a_headless_failure_alongside_an_interactive_one_still_fails() -> None:
    results = [
        _result("chip", surface=INTERACTIVE_SURFACE, passed=False),
        _result("slack", surface=HEADLESS_SURFACE, passed=False),
    ]
    lane = _ai_lane_result(results, backend="api", graded=True)
    assert not lane.passed
    assert "1 failed" in lane.detail


_NO_HISTORY_GATES: dict[str, bool | float] = {
    "persist": False,
    "baseline": False,
    "gate_regressions": False,
    "gate_cost_regression": False,
    "cost_regression_tolerance": 0.0,
    "gate_cost_bounds": False,
}


class TestTheSingleTrialFinalizeVerdict:
    """``finalize_single_run`` — the verdict of the DEFAULT ``t3 eval run`` shape.

    ``--trials 1`` with no ``--models`` is what ``eval-nightly``, ``eval-pr-reusable``
    and ``eval-ci-heal`` all drive, so this is the exemption's load-bearing point:
    without it an SDK bump still reds every metered leg.
    """

    def test_a_failing_interactive_scenario_does_not_red_the_run(self) -> None:
        results = [_result("chip", surface=INTERACTIVE_SURFACE, passed=False)]
        assert not finalize_single_run(results, specs=[r.spec for r in results], max_turns=None, **_NO_HISTORY_GATES)

    def test_a_failing_headless_scenario_still_reds_the_run(self) -> None:
        results = [_result("slack", surface=HEADLESS_SURFACE, passed=False)]
        assert finalize_single_run(results, specs=[r.spec for r in results], max_turns=None, **_NO_HISTORY_GATES)

    def test_a_headless_failure_alongside_an_interactive_one_still_reds(self) -> None:
        results = [
            _result("chip", surface=INTERACTIVE_SURFACE, passed=False),
            _result("slack", surface=HEADLESS_SURFACE, passed=False),
        ]
        assert finalize_single_run(results, specs=[r.spec for r in results], max_turns=None, **_NO_HISTORY_GATES)


def _interactive_tool_call_spec(name: str, *, surface: str) -> EvalSpec:
    """A spec that cannot pass without an ``AskUserQuestion`` call — the real shape.

    This is the matcher shape the catalog's interactive scenarios carry (and the one
    :func:`teatree.eval.surface.requires_interactive_tool_call` flags), so a run that
    captures no tool call grades FAIL exactly as the shipped scenario does under a
    bundled CLI that renders the call as a markdown chip.
    """
    return EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="ask the user where to file it",
        matchers=(
            Matcher(
                kind="positive",
                tool=INTERACTIVE_QUESTION_TOOL,
                arg_path="questions",
                operator="~",
                value="(?i)(upstream|overlay)",
            ),
        ),
        source_path=Path("synthetic.yaml"),
        surface=surface,
    )


class _NoToolCallRunner:
    """The bundled-CLI failure mode: a completed turn that captured no tool call."""

    def run(self, spec: EvalSpec) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=("Should I file it upstream or on the overlay?",),
            terminal_reason="success",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


def _drive_single_trial(
    specs: list[EvalSpec],
    *,
    monkeypatch: pytest.MonkeyPatch,
    escalation: EscalationConfig | None = None,
    summary_md: Path | None = None,
) -> int:
    """Run ``run_single_trial`` end to end against a stubbed runner; return the exit code."""
    runner = _NoToolCallRunner()
    monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *_a, **_k: runner)
    monkeypatch.setattr("teatree.cli.eval.single_trial.make_escalation_runner", lambda **_k: runner)
    try:
        run_single_trial(
            specs,
            backend="api",
            max_turns=None,
            transcript_dir=None,
            require_executed=False,
            max_budget_usd=1.0,
            effort=None,
            parallel=1,
            output_format="text",
            grader=None,
            judge=False,
            transcript_html=None,
            summary_md=summary_md,
            gates=SingleTrialGates(persist=False, baseline=False, gate_regressions=False, gate_cost_regression=False),
            escalation=escalation,
        )
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


class TestTheSingleTrialLaneEndToEnd:
    """The CI-observable behaviour: what ``t3 eval run`` actually exits with.

    Driven through the whole single-pass body (run → grade → render → guards →
    gate), because the exit code is what the metered workflow legs consume.
    """

    def test_an_interactive_scenario_failing_on_the_missing_tool_call_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        specs = [_interactive_tool_call_spec("chip", surface=INTERACTIVE_SURFACE)]
        assert _drive_single_trial(specs, monkeypatch=monkeypatch) == 0

    def test_the_same_scenario_on_the_headless_surface_still_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Anti-vacuity: the run really does grade FAIL — only the surface label
        # decides whether that failure gates.
        specs = [_interactive_tool_call_spec("slack", surface=HEADLESS_SURFACE)]
        assert _drive_single_trial(specs, monkeypatch=monkeypatch) == 1


class TestTheEscalationVerdict:
    """``--escalate-on-fail`` — the single-trial lane's OTHER exit (the PR lane's)."""

    def test_a_confirmed_interactive_failure_is_not_a_hard_red(self) -> None:
        spec = _spec("chip", surface=INTERACTIVE_SURFACE)
        initial = [_result("chip", surface=INTERACTIVE_SURFACE, passed=False)]
        report = escalate_failures(
            initial, lambda s: _result(s.name, surface=INTERACTIVE_SURFACE, passed=False), escalate_trials=2
        )
        assert not report.hard_red
        # Still REPORTED as confirmed — advisory is non-gating, not invisible.
        assert report.outcomes[0].classification == "confirmed"
        assert report.outcomes[0].advisory
        assert spec.name == report.outcomes[0].spec_name

    def test_a_confirmed_headless_failure_is_still_a_hard_red(self) -> None:
        initial = [_result("slack", surface=HEADLESS_SURFACE, passed=False)]
        report = escalate_failures(
            initial, lambda s: _result(s.name, surface=HEADLESS_SURFACE, passed=False), escalate_trials=2
        )
        assert report.hard_red
        assert not report.outcomes[0].advisory

    def test_the_lane_exits_zero_on_a_confirmed_interactive_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        specs = [_interactive_tool_call_spec("chip", surface=INTERACTIVE_SURFACE)]
        code = _drive_single_trial(specs, monkeypatch=monkeypatch, escalation=EscalationConfig(escalate_trials=2))
        assert code == 0

    def test_the_lane_still_exits_one_on_a_confirmed_headless_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        specs = [_interactive_tool_call_spec("slack", surface=HEADLESS_SURFACE)]
        code = _drive_single_trial(specs, monkeypatch=monkeypatch, escalation=EscalationConfig(escalate_trials=2))
        assert code == 1

    def test_the_escalation_summary_marks_the_advisory_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A confirmed advisory failure must read as confirmed-but-advisory, never be
        # laundered into "flaky" — that would hide a real interactive regression.
        summary = tmp_path / "summary.md"
        specs = [_interactive_tool_call_spec("chip", surface=INTERACTIVE_SURFACE)]
        _drive_single_trial(
            specs, monkeypatch=monkeypatch, escalation=EscalationConfig(escalate_trials=2), summary_md=summary
        )
        body = summary.read_text(encoding="utf-8")
        assert "1 confirmed, 0 flaky" in body
        assert "confirmed (advisory)" in body
