"""Persisting eval results into the durable run-history ledger.

Guards the boundary between the Django-free harness and ``EvalRunRecord``:
an ``any_of`` matcher result must persist as a valid ``MatcherDetail`` rather
than crashing on attributes ``AnyOf`` does not carry.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.eval.models import AnyOf, EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.persistence import persist_run
from teatree.eval.report import evaluate

_TASK_BRANCH = Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="pytest")
_BG_BASH_BRANCH = Matcher(kind="positive", tool="Bash", arg_path="run_in_background", operator="~", value="(?i)true")


def _spec(matchers: tuple[Matcher | AnyOf, ...]) -> EvalSpec:
    return EvalSpec(
        name="background_long_operations_full_suite",
        scenario="text",
        agent_path="skills/rules/SKILL.md",
        prompt="do",
        matchers=matchers,
        source_path=Path("/tmp/spec.yaml"),
    )


def _run(tool_calls: tuple[EvalToolCall, ...], *, cost_usd: float = 0.0) -> EvalRun:
    return EvalRun(
        spec_name="background_long_operations_full_suite",
        tool_calls=tool_calls,
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
    )


class TestPersistAnyOf(TestCase):
    def test_persists_passing_any_of_result(self) -> None:
        spec = _spec((AnyOf(alternatives=(_TASK_BRANCH, _BG_BASH_BRANCH)),))
        run = _run((EvalToolCall(name="Bash", input={"command": "uv run pytest", "run_in_background": True}, turn=1),))
        result = evaluate(spec, run)
        assert result.passed is True

        record = persist_run([result], model="haiku")

        scenario = record.scenario_results.get()
        assert scenario.verdict == "pass"
        detail = scenario.matcher_details[0]
        assert detail["kind"] == "any_of"
        assert detail["passed"] is True
        assert "Task" in detail["tool"]
        assert "Bash" in detail["tool"]
        assert "run_in_background" in detail["arg_path"]

    def test_persists_failing_any_of_result(self) -> None:
        spec = _spec((AnyOf(alternatives=(_TASK_BRANCH, _BG_BASH_BRANCH)),))
        run = _run((EvalToolCall(name="Bash", input={"command": "uv run pytest"}, turn=1),))
        result = evaluate(spec, run)
        assert result.passed is False

        record = persist_run([result], model="haiku")

        scenario = record.scenario_results.get()
        assert scenario.verdict == "fail"
        assert scenario.matcher_details[0]["kind"] == "any_of"
        assert scenario.matcher_details[0]["passed"] is False


class TestPersistCost(TestCase):
    def test_persist_run_stores_per_scenario_cost(self) -> None:
        spec = _spec((_TASK_BRANCH,))
        run = _run((EvalToolCall(name="Task", input={"prompt": "uv run pytest"}, turn=1),), cost_usd=0.17)
        result = evaluate(spec, run)

        record = persist_run([result], model="haiku")

        assert record.scenario_results.get().cost_usd == pytest.approx(0.17)

    def test_persist_run_defaults_unmetered_cost_to_zero(self) -> None:
        spec = _spec((_TASK_BRANCH,))
        run = _run((EvalToolCall(name="Task", input={"prompt": "uv run pytest"}, turn=1),))
        result = evaluate(spec, run)

        record = persist_run([result], model="haiku")

        assert record.scenario_results.get().cost_usd == pytest.approx(0.0)
