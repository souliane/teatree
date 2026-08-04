"""A cap-truncated FAIL whose matchers all passed is reported distinctly from non-compliance.

``ScenarioResult.passed`` returns ``False`` on a ``max_turns`` terminal reason even
when every matcher matched (#2192), so a harness problem — a sandbox the agent burned
its turn budget probing — is recorded with the same ``FAIL`` as a genuine behavioural
red. These pin that the two are distinguishable in every rendered surface, without
weakening the gate: the verdict stays ``fail``.
"""

import json
from pathlib import Path

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher, TokenUsage
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import MatcherResult, ScenarioResult, render_json, render_text
from teatree.eval.summary_markdown import render_summary_markdown

_MATCHER = Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="gh pr create")


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path="skills/ship/SKILL.md",
        prompt="p",
        matchers=(_MATCHER,),
        source_path=Path("evals/scenarios/ship_delivery.yaml"),
    )


def _run(name: str, *, terminal_reason: str) -> EvalRun:
    return EvalRun(
        spec_name=name,
        tool_calls=(EvalToolCall(name="Bash", input={"command": "gh pr create --fill"}, turn=1),),
        text_blocks=("opening the PR",),
        terminal_reason=terminal_reason,
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        usage=TokenUsage(),
    )


def _result(name: str, *, terminal_reason: str, matcher_passed: bool) -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name),
        run=_run(name, terminal_reason=terminal_reason),
        matcher_results=(
            MatcherResult(matcher=_MATCHER, passed=matcher_passed, message="" if matcher_passed else "x"),
        ),
        skipped=False,
    )


def test_cap_truncated_after_every_matcher_matched_is_flagged_but_still_fails() -> None:
    result = _result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=True)
    assert result.passed is False
    assert result.cap_truncated_matchers_satisfied is True


def test_a_genuine_matcher_failure_is_never_flagged_as_cap_truncated() -> None:
    result = _result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=False)
    assert result.cap_truncated_matchers_satisfied is False


def test_a_clean_matcher_failure_is_never_flagged() -> None:
    result = _result("ship_opens_pr", terminal_reason="success", matcher_passed=False)
    assert result.cap_truncated_matchers_satisfied is False


def test_text_report_separates_the_harness_class_from_non_compliance() -> None:
    capped = _result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=True)
    non_compliant = _result("ship_pushes_branch", terminal_reason="max_turns", matcher_passed=False)
    text = render_text([capped, non_compliant])
    capped_line, non_compliant_line = (
        next(line for line in text.splitlines() if line.startswith(("PASS", "FAIL")) and name in line)
        for name in ("ship_opens_pr", "ship_pushes_branch")
    )
    assert "cap-truncated" in capped_line
    assert "cap-truncated" not in non_compliant_line
    assert "every matcher passed" in text


def test_json_report_exposes_the_cap_truncated_channel() -> None:
    payload = json.loads(render_json([_result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=True)]))
    assert payload["scenarios"][0]["cap_truncated_matchers_satisfied"] is True
    assert payload["scenarios"][0]["passed"] is False


def test_summary_markdown_verdict_marks_the_cap_class() -> None:
    capped = _result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=True)
    non_compliant = _result("ship_pushes_branch", terminal_reason="max_turns", matcher_passed=False)
    markdown = render_summary_markdown([capped, non_compliant])
    assert "| ship_opens_pr | clean_room | fail (cap) |" in markdown
    assert "| ship_pushes_branch | clean_room | fail |" in markdown


def test_summary_markdown_counts_a_cap_marked_row_as_a_failure() -> None:
    markdown = render_summary_markdown([_result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=True)])
    assert "**0 passed**, **1 failed**" in markdown


def test_pass_at_k_row_marks_the_cap_class_when_every_red_trial_is_cap_truncated() -> None:
    capped = _result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=True)
    clean_pass = _result("ship_opens_pr", terminal_reason="success", matcher_passed=True)
    aggregate = PassAtKResult(
        spec_name="ship_opens_pr",
        trials=2,
        passes=1,
        require="all",
        skipped=False,
        terminal_reason="max_turns",
        trial_results=(capped, clean_pass),
    )
    assert aggregate.ok is False
    assert "| ship_opens_pr | unknown | fail (cap) | 1/2 |" in render_summary_markdown([aggregate])


def test_pass_at_k_row_stays_plain_fail_when_a_trial_genuinely_missed_the_matcher() -> None:
    capped = _result("ship_opens_pr", terminal_reason="max_turns", matcher_passed=True)
    missed = _result("ship_opens_pr", terminal_reason="success", matcher_passed=False)
    aggregate = PassAtKResult(
        spec_name="ship_opens_pr",
        trials=2,
        passes=0,
        require="any",
        skipped=False,
        terminal_reason="max_turns",
        trial_results=(capped, missed),
    )
    assert "| ship_opens_pr | unknown | fail | 0/2 |" in render_summary_markdown([aggregate])
