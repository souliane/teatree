"""A gate-carried pass is annotated `pass (gate-assisted)`, never a pass condition."""

import json
from pathlib import Path

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, GateEvent, Matcher
from teatree.eval.report import evaluate, render_json, render_text


def _spec() -> EvalSpec:
    return EvalSpec(
        name="hooked_scenario",
        scenario="a hooked scenario",
        agent_path="skills/rules/SKILL.md",
        prompt="ask the user",
        matchers=(Matcher(kind="positive", tool="AskUserQuestion", arg_path="questions", operator="~", value="."),),
        source_path=Path("spec.yaml"),
        production_hooks=True,
    )


def _run(*, gate_events: tuple[GateEvent, ...]) -> EvalRun:
    return EvalRun(
        spec_name="hooked_scenario",
        tool_calls=(EvalToolCall(name="AskUserQuestion", input={"questions": "which?"}, turn=2),),
        text_blocks=("asked",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        gate_events=gate_events,
    )


_STOP_BLOCK = GateEvent(hook_event_name="Stop", outcome="block", output_snippet="decision: block")


def test_gate_assisted_pass_renders_the_annotation() -> None:
    result = evaluate(_spec(), _run(gate_events=(_STOP_BLOCK,)))
    assert result.passed is True
    assert result.gate_assisted is True
    text = render_text([result])
    assert "PASS (gate-assisted) hooked_scenario" in text


def test_first_try_pass_with_no_gate_firing_renders_plain_pass() -> None:
    result = evaluate(_spec(), _run(gate_events=()))
    assert result.passed is True
    assert result.gate_assisted is False
    text = render_text([result])
    assert "PASS hooked_scenario" in text
    assert "gate-assisted" not in text


def test_gate_firing_is_not_a_pass_condition_a_non_stop_block_pass_stays_plain() -> None:
    non_stop = GateEvent(hook_event_name="PreToolUse", outcome="allow", output_snippet="")
    result = evaluate(_spec(), _run(gate_events=(non_stop,)))
    assert result.gate_assisted is False


def test_json_report_exposes_the_gate_channel() -> None:
    result = evaluate(_spec(), _run(gate_events=(_STOP_BLOCK,)))
    payload = json.loads(render_json([result]))
    scenario = payload["scenarios"][0]
    assert scenario["gate_assisted"] is True
    assert scenario["gate_events"][0]["hook_event"] == "Stop"
    assert scenario["gate_events"][0]["is_stop_block"] is True
