# test-path: cross-cutting
"""Single-action probe: a cap after the probe's contract is met is not a #2192 taint."""

from pathlib import Path

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.report import evaluate

_POS = Matcher(kind="positive", tool="Write", arg_path="file_path", operator="~", value=r"tests/.*test_.*\.py")
_NEG = Matcher(kind="negative", tool="Bash", arg_path="command", operator="contains", value="--no-verify")
_WRITE = (EvalToolCall(name="Write", input={"file_path": "tests/foo/test_x.py"}, turn=1),)


def _spec(*, single_action: bool = True, matchers: tuple[Matcher, ...] = (_POS,)) -> EvalSpec:
    return EvalSpec(
        name="probe",
        scenario="t",
        agent_path="skills/code/SKILL.md",
        prompt="single action then stop",
        matchers=matchers,
        source_path=Path("/tmp/probe.yaml"),
        single_action=single_action,
    )


def _run(
    *,
    tool_calls: tuple[EvalToolCall, ...] = (),
    terminal_reason: str = "max_turns",
    is_error: bool = False,
) -> EvalRun:
    return EvalRun(
        spec_name="probe",
        tool_calls=tool_calls,
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
    )


class TestSingleActionProbeVerdict:
    def test_positive_matched_then_max_turns_is_pass(self) -> None:
        assert evaluate(_spec(), _run(tool_calls=_WRITE, terminal_reason="max_turns")).passed

    def test_budget_and_timeout_caps_also_exempt(self) -> None:
        assert evaluate(_spec(), _run(tool_calls=_WRITE, terminal_reason="budget_exceeded")).passed
        assert evaluate(_spec(), _run(tool_calls=_WRITE, terminal_reason="timeout")).passed

    def test_no_positive_match_then_cap_fails(self) -> None:
        assert not evaluate(_spec(), _run(tool_calls=(), terminal_reason="max_turns")).passed

    def test_forbidden_call_then_cap_fails(self) -> None:
        bad = (*_WRITE, EvalToolCall(name="Bash", input={"command": "git commit --no-verify"}, turn=2))
        assert not evaluate(_spec(matchers=(_POS, _NEG)), _run(tool_calls=bad, terminal_reason="max_turns")).passed

    def test_error_cap_still_fails(self) -> None:
        assert not evaluate(_spec(), _run(tool_calls=_WRITE, terminal_reason="error_max_turns", is_error=True)).passed

    def test_non_single_action_cap_still_taints(self) -> None:
        assert not evaluate(_spec(single_action=False), _run(tool_calls=_WRITE, terminal_reason="max_turns")).passed

    def test_no_cap_passes_normally(self) -> None:
        assert evaluate(_spec(), _run(tool_calls=_WRITE, terminal_reason="success")).passed
