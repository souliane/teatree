"""Evaluation and report rendering for ``ScenarioResult``."""

import json
from pathlib import Path

import pytest

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.report import MatcherResult, ScenarioResult, evaluate, render_json, render_text


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
        assert payload["summary"] == {"total": 1, "passed": 1, "failed": 0, "skipped": 0}
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
