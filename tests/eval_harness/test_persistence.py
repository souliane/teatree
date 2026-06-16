"""Persisting eval results into the durable run-history ledger.

Guards the boundary between the Django-free harness and ``EvalRunRecord``:
an ``any_of`` matcher result must persist as a valid ``MatcherDetail`` rather
than crashing on attributes ``AnyOf`` does not carry.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.eval.matrix import MatrixRow
from teatree.eval.models import AnyOf, EvalRun, EvalSpec, EvalToolCall, FinalStateMatcher, Matcher, TokenUsage
from teatree.eval.persistence import persist_matrix, persist_run
from teatree.eval.report import evaluate

_TASK_BRANCH = Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="pytest")
_BG_BASH_BRANCH = Matcher(kind="positive", tool="Bash", arg_path="run_in_background", operator="~", value="(?i)true")


def _spec(matchers: tuple[Matcher | AnyOf | FinalStateMatcher, ...]) -> EvalSpec:
    return EvalSpec(
        name="background_long_operations_full_suite",
        scenario="text",
        agent_path="skills/rules/SKILL.md",
        prompt="do",
        matchers=matchers,
        source_path=Path("/tmp/spec.yaml"),
    )


def _run(  # noqa: PLR0913 — test-data builder mirroring the EvalRun dataclass fields.
    tool_calls: tuple[EvalToolCall, ...],
    *,
    text_blocks: tuple[str, ...] = (),
    cost_usd: float = 0.0,
    usage: TokenUsage | None = None,
    main_cost_usd: float = 0.0,
    aux_cost_usd: float = 0.0,
) -> EvalRun:
    return EvalRun(
        spec_name="background_long_operations_full_suite",
        tool_calls=tool_calls,
        text_blocks=text_blocks,
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
        usage=usage if usage is not None else TokenUsage(),
        main_cost_usd=main_cost_usd,
        aux_cost_usd=aux_cost_usd,
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


class TestPersistFinalState(TestCase):
    def test_persists_final_state_matcher_detail(self) -> None:
        spec = _spec((FinalStateMatcher(operator="contains", value="pushed"),))
        run = _run((), text_blocks=("All done — the branch is pushed.",))
        result = evaluate(spec, run)
        assert result.passed is True

        record = persist_run([result], model="haiku")

        detail = record.scenario_results.get().matcher_details[0]
        assert detail["kind"] == "final_state"
        assert detail["operator"] == "contains"
        assert detail["value"] == "pushed"
        assert detail["passed"] is True


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


class TestPersistTokens(TestCase):
    def test_persist_run_stores_token_columns(self) -> None:
        spec = _spec((_TASK_BRANCH,))
        run = _run(
            (EvalToolCall(name="Task", input={"prompt": "uv run pytest"}, turn=1),),
            cost_usd=0.17,
            usage=TokenUsage(input=120, cache_creation=340, cache_read=6500, output=80),
        )
        result = evaluate(spec, run)

        record = persist_run([result], model="haiku")

        scenario = record.scenario_results.get()
        assert scenario.input_tokens == 120
        assert scenario.cache_creation_tokens == 340
        assert scenario.cache_read_tokens == 6500
        assert scenario.output_tokens == 80

    def test_persist_matrix_stores_token_columns_from_row_usage(self) -> None:
        rows = [
            MatrixRow(
                scenario="alpha",
                model="m",
                passed=True,
                score=1.0,
                trials=1,
                skipped=False,
                cost_usd=0.10,
                usage=TokenUsage(input=10, cache_creation=20, cache_read=70, output=5),
            ),
        ]

        record = persist_matrix(rows, models=["m"])

        scenario = record.scenario_results.get()
        assert scenario.input_tokens == 10
        assert scenario.cache_creation_tokens == 20
        assert scenario.cache_read_tokens == 70
        assert scenario.output_tokens == 5


class TestPersistMainAuxCost(TestCase):
    def test_persist_run_stores_main_and_aux_cost(self) -> None:
        spec = _spec((_TASK_BRANCH,))
        run = _run(
            (EvalToolCall(name="Task", input={"prompt": "uv run pytest"}, turn=1),),
            cost_usd=0.52,
            main_cost_usd=0.5,
            aux_cost_usd=0.02,
        )
        record = persist_run([evaluate(spec, run)], model="claude-opus-4-8")

        scenario = record.scenario_results.get()
        assert scenario.main_cost_usd == pytest.approx(0.5)
        assert scenario.aux_cost_usd == pytest.approx(0.02)

    def test_persist_matrix_stores_main_and_aux_cost_from_row(self) -> None:
        rows = [
            MatrixRow(
                scenario="alpha",
                model="m",
                passed=True,
                score=1.0,
                trials=1,
                skipped=False,
                cost_usd=0.31,
                main_cost_usd=0.3,
                aux_cost_usd=0.01,
            ),
        ]
        record = persist_matrix(rows, models=["m"])

        scenario = record.scenario_results.get()
        assert scenario.main_cost_usd == pytest.approx(0.3)
        assert scenario.aux_cost_usd == pytest.approx(0.01)


class TestPersistMatrixErroredCells(TestCase):
    def test_errored_cell_is_not_persisted_as_a_fail_row(self) -> None:
        rows = [
            MatrixRow(scenario="alpha", model="m", passed=True, score=1.0, trials=1, skipped=False, cost_usd=0.10),
            MatrixRow(scenario="beta", model="m", passed=False, score=0.0, trials=1, skipped=False, errored=True),
        ]

        record = persist_matrix(rows, models=["m"])

        persisted = {(r.scenario_name, r.verdict) for r in record.scenario_results.all()}
        # The errored cell is a transient infra blip, not a graded FAIL — it must
        # not land in the ledger as a fail row that would lower the baseline pass-rate.
        assert ("alpha", "pass") in persisted
        assert not any(name == "beta" for name, _ in persisted)
        assert record.failed == 0
        assert record.passed == 1
