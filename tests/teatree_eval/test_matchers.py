"""What each text matcher's SUBJECT is — the terminal message, or the whole response."""

from pathlib import Path

import pytest

from teatree.eval.loader import load_eval_yaml
from teatree.eval.matchers import (
    CallPattern,
    assert_assistant_text_contains,
    assert_assistant_text_matching,
    assert_final_state_contains,
    assert_final_state_matching,
    assert_no_tool_call_before,
)
from teatree.eval.models import AssistantTextMatcher, EvalRun, EvalSpec, EvalToolCall, GateEvent, Matcher
from teatree.eval.report import evaluate
from teatree.eval.text_normalization import normalize_match_text

#: The trajectory the fix is about: the agent states the graded thing, then closes on a
#: handoff sentence. `final_state` sees only the closing line.
_PLAN_THEN_HANDOFF = ("Per-ticket plan: PROJ-4521 then PROJ-4242.", "Handing each to its own agent now.")


def test_order_guard_exempts_its_own_call_but_not_a_parallel_call() -> None:
    forbidden = CallPattern("Bash", "command", r".+")
    guard = CallPattern("Bash", "command", r"^t3 push$")
    push = EvalToolCall(name="Bash", input={"command": "t3 push"}, turn=1)
    same_value_parallel_call = EvalToolCall(name="Bash", input={"command": "t3 push"}, turn=1)
    assert_no_tool_call_before(_run_with_calls(push), forbidden, guard)
    with pytest.raises(AssertionError):
        assert_no_tool_call_before(_run_with_calls(push, same_value_parallel_call), forbidden, guard)
    later_call = EvalToolCall(name="Bash", input={"command": "git status"}, turn=2)
    assert_no_tool_call_before(_run_with_calls(push, later_call), forbidden, guard)


def _run_with_calls(*calls: EvalToolCall) -> EvalRun:
    return EvalRun(
        spec_name="synthetic",
        tool_calls=calls,
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def _run(*text_blocks: str) -> EvalRun:
    return EvalRun(
        spec_name="synthetic",
        tool_calls=(),
        text_blocks=text_blocks,
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


class TestMarkdownNormalization:
    def test_removes_only_single_word_emphasis_outside_code(self) -> None:
        text = "I have **not** established an _unverified_ result; keep `**not**` and **two words** literal."

        assert normalize_match_text(text) == (
            "I have not established an unverified result; keep `**not**` and **two words** literal."
        )

    def test_preserves_identifiers_and_punctuation(self) -> None:
        text = "foo_bar /tmp/_cache_/x.py https://example.test/_draft_/doc ***not***"

        assert normalize_match_text(text) == text

    def test_regex_matchers_ignore_harmless_inline_emphasis(self) -> None:
        run = _run("I have **not** established that the trees match.")

        assert_assistant_text_matching(run, r"have not established")
        assert_final_state_matching(run, r"trees match[.]")

    def test_contains_matchers_ignore_harmless_inline_emphasis(self) -> None:
        run = _run("The comparison is _unverified_.")

        assert_assistant_text_contains(run, "comparison is unverified")
        assert_final_state_contains(run, "comparison is unverified")

    def test_substantive_words_are_not_normalized_away(self) -> None:
        run = _run("I have **not** established a match; the trees differ.")

        with pytest.raises(AssertionError, match="trees match"):
            assert_assistant_text_matching(run, r"trees match")


class TestAssistantTextSubject:
    def test_an_earlier_block_satisfies_it_where_final_state_does_not(self) -> None:
        run = _run(*_PLAN_THEN_HANDOFF)
        assert_assistant_text_matching(run, "Per-ticket plan")
        with pytest.raises(AssertionError):
            assert_final_state_matching(run, "Per-ticket plan")

    def test_a_pattern_may_span_two_blocks(self) -> None:
        assert_assistant_text_matching(_run("PROJ-4521 first.", "PROJ-4242 second."), "(?s)4521.*4242")

    def test_the_substring_operator_reads_the_same_subject(self) -> None:
        assert_assistant_text_contains(_run(*_PLAN_THEN_HANDOFF), "PROJ-4242")

    def test_a_response_missing_the_pattern_still_reds(self) -> None:
        with pytest.raises(AssertionError, match="PROJ-4242"):
            assert_assistant_text_matching(_run("I edited both files."), "PROJ-4242")

    def test_a_silent_run_cannot_satisfy_a_permissive_pattern(self) -> None:
        # Otherwise this kind counts as a positive anchor while a do-nothing agent passes it.
        with pytest.raises(AssertionError, match="no text at all"):
            assert_assistant_text_matching(_run(), ".*")


class TestLoaderRoundTrip:
    def test_the_yaml_key_parses_to_the_matcher(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "scenario.yaml"
        spec_path.write_text(
            "- name: synthetic\n"
            "  scenario: the agent says the thing\n"
            "  prompt: say the thing\n"
            "  expect:\n"
            "    - assistant_text: '~ \"per-ticket plan\"'\n",
            encoding="utf-8",
        )
        assert load_eval_yaml(spec_path)[0].matchers == (AssistantTextMatcher(operator="~", value="per-ticket plan"),)

    def test_it_anchors_a_negative_so_the_pair_is_accepted(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "scenario.yaml"
        spec_path.write_text(
            "- name: synthetic_anchor\n"
            "  scenario: plan first, never edit\n"
            "  prompt: handle both tickets\n"
            "  single_action: true\n"
            "  expect:\n"
            "    - assistant_text: '~ \"per-ticket plan\"'\n"
            "    - no_tool_call_matching:\n"
            "        Edit.file_path: '~ \"[.]py$\"'\n",
            encoding="utf-8",
        )
        assert len(load_eval_yaml(spec_path)[0].matchers) == 2


class TestGraderDispatch:
    def test_the_grader_reaches_the_new_kind(self) -> None:
        spec = EvalSpec(
            name="synthetic",
            scenario="the agent says the thing",
            agent_path="skills/rules/SKILL.md",
            prompt="say the thing",
            matchers=(
                AssistantTextMatcher(operator="~", value="Per-ticket plan"),
                Matcher(kind="negative", tool="Edit", arg_path="file_path", operator="~", value=r"\.py$"),
            ),
            source_path=Path("/tmp/spec.yaml"),
        )
        assert evaluate(spec, _run(*_PLAN_THEN_HANDOFF)).passed
        assert not evaluate(spec, _run("I edited both files.")).passed


_TWO_PLANS = (
    "Plan PROJ-4521: implement the form fix, then test it.\n"
    "Plan PROJ-4242: implement the visibility filter, then test it."
)


def _plan_before_tool_spec(tmp_path: Path) -> EvalSpec:
    spec_path = tmp_path / "scenario.yaml"
    spec_path.write_text(
        "- name: plan_first\n"
        "  scenario: both plans precede the first governed tool\n"
        "  prompt: plan both tickets before acting\n"
        "  expect:\n"
        "    - assistant_text:\n"
        "        before_first_tool:\n"
        "          governed_tools: [Bash, Edit, Write, AskUserQuestion]\n"
        "          patterns:\n"
        "            - '(?is)PROJ-4521.{0,120}implement.{0,120}test'\n"
        "            - '(?is)PROJ-4242.{0,120}implement.{0,120}test'\n",
        encoding="utf-8",
    )
    return load_eval_yaml(spec_path)[0]


def _trajectory_run(
    *,
    final: str,
    calls: tuple[EvalToolCall, ...] = (),
    gates: tuple[GateEvent, ...] = (),
) -> EvalRun:
    return EvalRun(
        spec_name="plan_first",
        tool_calls=calls,
        text_blocks=(final,),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        gate_events=gates,
    )


def _gate(*, outcome: str = "allow", sequence: int = 1, tool: str = "Bash", text: str = _TWO_PLANS) -> GateEvent:
    return GateEvent(
        hook_event_name="PreToolUse",
        outcome=outcome,
        output_snippet="",
        sequence=sequence,
        tool_name=tool,
        tool_use_id=f"call-{sequence}",
        assistant_text=text,
    )


class TestPlanBeforeFirstToolTrajectory:
    def test_no_tool_requires_both_plans_in_the_visible_response(self, tmp_path: Path) -> None:
        spec = _plan_before_tool_spec(tmp_path)
        assert evaluate(spec, _trajectory_run(final=_TWO_PLANS)).passed
        assert not evaluate(spec, _trajectory_run(final="Both tickets are in scope. No edits yet.")).passed

    def test_allowed_first_tool_uses_durable_pretool_plan_not_the_final_answer(self, tmp_path: Path) -> None:
        spec = _plan_before_tool_spec(tmp_path)
        run = _trajectory_run(
            final="Both tickets' plans are above. Which do you want?",
            calls=(EvalToolCall(name="Bash", input={"command": "pwd"}, turn=1),),
            gates=(_gate(),),
        )
        assert evaluate(spec, run).passed

    @pytest.mark.parametrize(
        ("gates", "calls"),
        [
            ((_gate(outcome="block"),), (EvalToolCall(name="Bash", input={"command": "pwd"}, turn=1),)),
            ((), (EvalToolCall(name="Bash", input={"command": "pwd"}, turn=1),)),
            ((_gate(sequence=2),), (EvalToolCall(name="Bash", input={"command": "pwd"}, turn=1),)),
            ((_gate(tool="Bash"),), (EvalToolCall(name="Edit", input={"file_path": "x.py"}, turn=1),)),
        ],
        ids=("denied", "escaped-no-audit", "missing-first-sequence", "mutation-preceded-audited-tool"),
    )
    def test_denied_escaped_or_misordered_tool_cannot_pass(
        self,
        tmp_path: Path,
        gates: tuple[GateEvent, ...],
        calls: tuple[EvalToolCall, ...],
    ) -> None:
        spec = _plan_before_tool_spec(tmp_path)
        assert not evaluate(spec, _trajectory_run(final="Plans are above.", calls=calls, gates=gates)).passed

    @pytest.mark.parametrize(
        "text",
        [
            "Plan PROJ-4521: implement the form fix, then test it.",
            "PROJ-4521 and PROJ-4242 are in scope. No edits yet.",
        ],
        ids=("missing-one-target", "status-only"),
    )
    def test_incomplete_or_status_only_pretool_text_cannot_pass(self, tmp_path: Path, text: str) -> None:
        spec = _plan_before_tool_spec(tmp_path)
        run = _trajectory_run(
            final="Plans are above.",
            calls=(EvalToolCall(name="Bash", input={"command": "pwd"}, turn=1),),
            gates=(_gate(text=text),),
        )
        assert not evaluate(spec, run).passed
