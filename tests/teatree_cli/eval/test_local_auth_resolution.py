"""The metered benchmark + multi-trial lanes resolve the API key themselves.

The single-trial ``t3 eval run`` lane builds its metered runner through
``teatree.eval.backends.make_runner``, the only non-Docker path that resolves the
metered ``ANTHROPIC_API_KEY`` via ``AnthropicApiKeyCredential().export()`` (env
wins, else exported from the ``pass`` store). The ``t3 eval benchmark`` and
``t3 eval run --trials k`` lanes must do the SAME — on a host ``--local`` run the
key lives only in ``pass``, so a lane that builds ``ApiInProcessRunner`` directly
leaves the isolated ``claude`` child unauthenticated and the run reports a
zero-cost auth failure.

These tests pin that each metered lane resolves the API key exactly where its
runner is constructed. RED on the pre-fix direct construction (never resolves the
key), GREEN once the lanes route through ``make_runner``.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from teatree.cli.eval.benchmark import benchmark
from teatree.cli.eval.multi_trial import run_model_matrix_lane, run_pass_at_k_lane
from teatree.eval.models import EvalRun, EvalSpec
from teatree.llm.credentials import AnthropicApiKeyCredential


def _spec(name: str = "s", model: str = "claude-opus-4-8") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        model=model,
    )


class _StubRunner:
    """Records that it was built; grades every scenario as a metered pass."""

    def __init__(self, **_: Any) -> None:
        pass

    def run(self, spec: EvalSpec) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=(),
            terminal_reason="end_turn",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.02,
        )


class TestPassAtKLaneResolvesApiKey:
    def test_pass_at_k_lane_resolves_the_api_key_before_metering(self) -> None:
        with (
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test") as ensure,
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        ):
            run_pass_at_k_lane(
                [_spec()],
                max_turns=None,
                trials=3,
                require="any",
                output_format="json",
            )
        ensure.assert_called_once_with()


class TestMatrixLaneResolvesApiKey:
    def test_matrix_lane_resolves_the_api_key_before_metering(self) -> None:
        with (
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test") as ensure,
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        ):
            run_model_matrix_lane(
                [_spec()],
                models="claude-opus-4-8,claude-sonnet-4-6",
                max_turns=None,
                trials=1,
                require="any",
                output_format="json",
                persist=False,
                baseline=False,
                gate_regressions=False,
            )
        ensure.assert_called_once_with()


class TestBenchmarkLaneResolvesApiKey:
    def test_benchmark_lane_resolves_the_api_key_before_metering(self) -> None:
        with (
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test") as ensure,
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
            patch("teatree.cli.eval.benchmark.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.cli.eval.benchmark.should_route_to_docker", return_value=False),
            patch("teatree.cli.eval.benchmark.persist_matrix_run"),
        ):
            benchmark(
                models="claude-opus-4-8@xhigh",
                scenarios=None,
                trials=1,
                max_turns=None,
                max_budget_usd=2.0,
                output_format="json",
                persist=False,
                local=True,
            )
        ensure.assert_called_once_with()
