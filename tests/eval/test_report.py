"""Evaluation and report rendering for ``ScenarioResult``."""

import dataclasses
import json
from pathlib import Path

import pytest

from teatree.eval.models import AnyOf, EvalRun, EvalSpec, EvalToolCall, JudgeSpec, Matcher
from teatree.eval.report import (
    JudgeOutcome,
    MatcherResult,
    ScenarioResult,
    evaluate,
    render_html,
    render_json,
    render_text,
)

_TASK_BRANCH = Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="pytest")
_BG_BASH_BRANCH = Matcher(kind="positive", tool="Bash", arg_path="run_in_background", operator="~", value="(?i)true")
_ANY_OF = AnyOf(alternatives=(_TASK_BRANCH, _BG_BASH_BRANCH))


def _spec(
    *,
    name: str = "scenario_one",
    matchers: tuple[Matcher, ...] = (
        Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
    ),
) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="text",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=matchers,
        source_path=Path("/tmp/spec.yaml"),
    )


def _run(
    *,
    spec_name: str = "scenario_one",
    tool_calls: tuple[EvalToolCall, ...] = (),
    terminal_reason: str = "success",
    is_error: bool = False,
    raw_stderr: str = "",
) -> EvalRun:
    return EvalRun(
        spec_name=spec_name,
        tool_calls=tool_calls,
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout="",
        raw_stderr=raw_stderr,
    )


class TestEvaluate:
    def test_returns_skipped_result_when_runner_skipped(self) -> None:
        spec = _spec()
        run = _run(terminal_reason="skipped: claude not on PATH")
        result = evaluate(spec, run)
        assert result.skipped is True
        assert result.passed is True
        assert result.matcher_results == ()

    def test_passes_when_positive_matcher_finds_call(self) -> None:
        spec = _spec()
        run = _run(
            tool_calls=(EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),),
        )
        result = evaluate(spec, run)
        assert result.skipped is False
        assert result.passed is True
        assert all(m.passed for m in result.matcher_results)

    def test_fails_when_positive_matcher_finds_nothing(self) -> None:
        spec = _spec()
        run = _run(tool_calls=())
        result = evaluate(spec, run)
        assert result.passed is False
        assert len(result.matcher_results) == 1
        assert result.matcher_results[0].passed is False
        assert "Bash" in result.matcher_results[0].message

    def test_any_of_passes_when_bg_bash_branch_matches_not_task(self) -> None:
        # The documented `Bash run_in_background: true` escape satisfies the
        # disjunction even though no Task was dispatched (the over-fit fix).
        spec = _spec(matchers=(_ANY_OF,))
        run = _run(
            tool_calls=(
                EvalToolCall(name="Bash", input={"command": "uv run pytest", "run_in_background": True}, turn=1),
            ),
        )
        result = evaluate(spec, run)
        assert result.passed is True

    def test_any_of_passes_when_task_branch_matches_not_bash(self) -> None:
        spec = _spec(matchers=(_ANY_OF,))
        run = _run(tool_calls=(EvalToolCall(name="Task", input={"prompt": "run the pytest suite"}, turn=1),))
        assert evaluate(spec, run).passed is True

    def test_any_of_fails_when_no_branch_matches(self) -> None:
        # A blocking FOREGROUND pytest (no run_in_background, no Task) fails.
        spec = _spec(matchers=(_ANY_OF,))
        run = _run(tool_calls=(EvalToolCall(name="Bash", input={"command": "uv run pytest"}, turn=1),))
        result = evaluate(spec, run)
        assert result.passed is False
        assert "ANY of 2 alternatives" in result.matcher_results[0].message

    def test_any_of_fails_against_noop_transcript(self) -> None:
        spec = _spec(matchers=(_ANY_OF,))
        assert evaluate(spec, _run(tool_calls=())).passed is False


class TestVerdict:
    def test_skip_maps_to_skip(self) -> None:
        result = evaluate(_spec(), _run(terminal_reason="skipped: claude not on PATH"))
        assert result.verdict == "skip"

    def test_pass_maps_to_pass(self) -> None:
        run = _run(tool_calls=(EvalToolCall(name="Bash", input={"command": "git worktree add x"}, turn=1),))
        assert evaluate(_spec(), run).verdict == "pass"

    def test_fail_maps_to_fail(self) -> None:
        assert evaluate(_spec(), _run(tool_calls=())).verdict == "fail"

    def test_canonicalizes_lowercase_bash_to_capitalized(self) -> None:
        # Loader emits lowercase `bash` from YAML; report should match against
        # the canonical `Bash` tool name in the captured calls.
        spec = _spec(
            matchers=(Matcher(kind="positive", tool="bash", arg_path="command", operator="contains", value="ls"),),
        )
        run = _run(
            tool_calls=(EvalToolCall(name="Bash", input={"command": "ls /tmp"}, turn=1),),
        )
        result = evaluate(spec, run)
        assert result.passed is True

    def test_negative_matcher_passes_when_no_match(self) -> None:
        spec = _spec(
            matchers=(Matcher(kind="negative", tool="Bash", arg_path="command", operator="~", value=r"Edit.*README"),),
        )
        run = _run(
            tool_calls=(EvalToolCall(name="Bash", input={"command": "git worktree add"}, turn=1),),
        )
        result = evaluate(spec, run)
        assert result.passed is True

    def test_negative_matcher_fails_when_pattern_matches(self) -> None:
        spec = _spec(
            matchers=(Matcher(kind="negative", tool="Bash", arg_path="command", operator="~", value=r"Edit.*README"),),
        )
        run = _run(
            tool_calls=(EvalToolCall(name="Bash", input={"command": "Edit /repo/README.md"}, turn=1),),
        )
        result = evaluate(spec, run)
        assert result.passed is False

    def test_failed_is_when_run_errored_even_with_passing_matchers(self) -> None:
        spec = _spec(matchers=())
        run = _run(terminal_reason="error_max_turns", is_error=True)
        result = evaluate(spec, run)
        # No matchers + run errored → passed is False (is_error path).
        assert result.passed is False

    def test_raises_for_unsupported_matcher_shape(self) -> None:
        spec = _spec(
            matchers=(Matcher(kind="weird", tool="Bash", arg_path="command", operator="??", value="x"),),
        )
        run = _run(
            tool_calls=(EvalToolCall(name="Bash", input={"command": "x"}, turn=1),),
        )
        with pytest.raises(NotImplementedError):
            evaluate(spec, run)


class TestRenderText:
    def test_emits_pass_line_and_summary(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
        )
        text = render_text([result])
        assert text.startswith("PASS scenario_one")
        assert "1 passed" in text
        assert "0 failed" in text

    def test_emits_skip_line_and_summary(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(terminal_reason="skipped: claude not on PATH"),
            matcher_results=(),
            skipped=True,
        )
        text = render_text([result])
        assert "SKIP scenario_one" in text
        assert "1 skipped" in text

    def test_emits_fail_lines_with_matcher_messages(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=False, message="no Bash call found"),),
            skipped=False,
        )
        text = render_text([result])
        assert "FAIL scenario_one" in text
        assert "no Bash call found" in text

    def test_emits_runtime_error_line_when_no_matcher_failures(self) -> None:
        # Run errored but no matchers failed (because there were no matchers
        # to fail) → the renderer should surface the run error explicitly.
        spec = _spec(matchers=())
        result = ScenarioResult(
            spec=spec,
            run=_run(terminal_reason="error_max_turns", is_error=True, raw_stderr="boom!"),
            matcher_results=(),
            skipped=False,
        )
        text = render_text([result])
        assert "run errored: error_max_turns" in text
        assert "stderr: boom!" in text


class TestRenderJson:
    def test_serializes_pass_and_summary(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(
                tool_calls=(EvalToolCall(name="Bash", input={"command": "ls"}, turn=1),),
            ),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
        )
        payload = json.loads(render_json([result]))
        summary = payload["summary"]
        assert summary["total"] == 1
        assert summary["passed"] == 1
        assert summary["failed"] == 0
        assert summary["skipped"] == 0
        [scenario] = payload["scenarios"]
        assert scenario["name"] == "scenario_one"
        assert scenario["passed"] is True
        assert scenario["tool_calls"] == [{"name": "Bash", "input": {"command": "ls"}, "turn": 1}]
        assert scenario["matchers"][0]["passed"] is True

    def test_serializes_failed_matcher(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=False, message="missed"),),
            skipped=False,
        )
        payload = json.loads(render_json([result]))
        assert payload["summary"]["failed"] == 1
        assert payload["scenarios"][0]["matchers"][0]["message"] == "missed"

    def test_serializes_any_of_matcher_with_alternatives(self) -> None:
        spec = _spec(matchers=(_ANY_OF,))
        result = ScenarioResult(
            spec=spec,
            run=_run(),
            matcher_results=(MatcherResult(matcher=_ANY_OF, passed=False, message="all failed"),),
            skipped=False,
        )
        payload = json.loads(render_json([result]))
        matcher = payload["scenarios"][0]["matchers"][0]
        assert matcher["kind"] == "any_of"
        assert len(matcher["alternatives"]) == 2
        assert matcher["alternatives"][1]["arg_path"] == "run_in_background"
        assert matcher["passed"] is False


class TestRenderHtml:
    def test_emits_self_contained_document_with_inline_style(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
        )
        html = render_html([result])
        assert html.lstrip().lower().startswith("<!doctype html>")
        assert "<style>" in html
        assert 'src="http' not in html
        assert 'href="http' not in html
        assert "scenario_one" in html

    def test_renders_summary_counts(self) -> None:
        spec = _spec()
        passing = ScenarioResult(
            spec=spec,
            run=_run(tool_calls=(EvalToolCall(name="Bash", input={"command": "ls"}, turn=1),)),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
        )
        failing = ScenarioResult(
            spec=_spec(name="scenario_two"),
            run=_run(spec_name="scenario_two"),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=False, message="missed"),),
            skipped=False,
        )
        html = render_html([passing, failing])
        assert "1 passed" in html
        assert "1 failed" in html

    def test_escapes_scenario_name_against_injection(self) -> None:
        spec = _spec(name="<script>alert(1)</script>")
        result = ScenarioResult(
            spec=spec,
            run=_run(spec_name="<script>alert(1)</script>"),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
        )
        html = render_html([result])
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_escapes_matcher_message_and_terminal_reason(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(terminal_reason="boom <b>&"),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=False, message="missed <tag> & more"),),
            skipped=False,
        )
        html = render_html([result])
        assert "missed <tag> & more" not in html
        assert "missed &lt;tag&gt; &amp; more" in html
        assert "boom &lt;b&gt;&amp;" in html

    def test_renders_skip_row(self) -> None:
        spec = _spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(terminal_reason="skipped: claude not on PATH"),
            matcher_results=(),
            skipped=True,
        )
        html = render_html([result])
        assert "1 skipped" in html
        assert "skipped: claude not on PATH" in html

    def test_renders_run_error_and_escaped_stderr(self) -> None:
        spec = _spec(matchers=())
        result = ScenarioResult(
            spec=spec,
            run=_run(terminal_reason="error_max_turns", is_error=True, raw_stderr="boom <fatal> & out"),
            matcher_results=(),
            skipped=False,
        )
        html = render_html([result])
        assert "run errored:" in html
        assert "error_max_turns" in html
        assert "boom <fatal> & out" not in html
        assert "boom &lt;fatal&gt; &amp; out" in html

    def test_renders_run_error_without_stderr(self) -> None:
        spec = _spec(matchers=())
        result = ScenarioResult(
            spec=spec,
            run=_run(terminal_reason="error_max_turns", is_error=True, raw_stderr=""),
            matcher_results=(),
            skipped=False,
        )
        html = render_html([result])
        assert "run errored:" in html
        assert "<pre>" not in html

    def test_renders_judge_rationale_escaped(self) -> None:
        spec = dataclasses.replace(_spec(), judge=JudgeSpec(rubric="faithful"))
        result = ScenarioResult(
            spec=spec,
            run=_run(),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
            judge=JudgeOutcome(passed=False, skipped=False, rationale="omitted the <migration> & step"),
        )
        html = render_html([result])
        assert "omitted the &lt;migration&gt; &amp; step" in html


def _judged_spec() -> EvalSpec:
    return dataclasses.replace(_spec(), judge=JudgeSpec(rubric="the explanation is faithful"))


_PASS_CALL = (EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),)


class TestJudgeIntegration:
    def test_judge_not_invoked_without_judge_block(self) -> None:
        spec = _spec()
        calls = {"n": 0}

        def _grader(_spec: EvalSpec, _run: EvalRun) -> JudgeOutcome:
            calls["n"] += 1
            return JudgeOutcome(passed=True, skipped=False, rationale="")

        result = evaluate(spec, _run(tool_calls=_PASS_CALL), judge=_grader)
        assert calls["n"] == 0
        assert result.judge is None

    def test_judge_failure_fails_scenario_even_when_matchers_pass(self) -> None:
        spec = _judged_spec()

        def _grader(_spec: EvalSpec, _run: EvalRun) -> JudgeOutcome:
            return JudgeOutcome(passed=False, skipped=False, rationale="unfaithful")

        result = evaluate(spec, _run(tool_calls=_PASS_CALL), judge=_grader)
        assert result.passed is False
        assert result.judge is not None
        assert result.judge.passed is False

    def test_judge_pass_with_matchers_pass_is_pass(self) -> None:
        spec = _judged_spec()

        def _grader(_spec: EvalSpec, _run: EvalRun) -> JudgeOutcome:
            return JudgeOutcome(passed=True, skipped=False, rationale="faithful")

        result = evaluate(spec, _run(tool_calls=_PASS_CALL), judge=_grader)
        assert result.passed is True

    def test_skipped_judge_does_not_fail_scenario(self) -> None:
        spec = _judged_spec()

        def _grader(_spec: EvalSpec, _run: EvalRun) -> JudgeOutcome:
            return JudgeOutcome(passed=False, skipped=True, rationale="claude missing")

        result = evaluate(spec, _run(tool_calls=_PASS_CALL), judge=_grader)
        assert result.passed is True

    def test_judge_rationale_in_text_report_on_failure(self) -> None:
        spec = _judged_spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(tool_calls=_PASS_CALL),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
            judge=JudgeOutcome(passed=False, skipped=False, rationale="omitted the migration"),
        )
        text = render_text([result])
        assert "judge: omitted the migration" in text

    def test_judge_in_json_report(self) -> None:
        spec = _judged_spec()
        result = ScenarioResult(
            spec=spec,
            run=_run(tool_calls=_PASS_CALL),
            matcher_results=(MatcherResult(matcher=spec.matchers[0], passed=True, message=""),),
            skipped=False,
            judge=JudgeOutcome(passed=True, skipped=False, rationale="faithful"),
        )
        payload = json.loads(render_json([result]))
        assert payload["scenarios"][0]["judge"] == {"passed": True, "skipped": False, "rationale": "faithful"}
