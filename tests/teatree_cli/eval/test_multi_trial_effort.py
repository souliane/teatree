"""Lane-level ``--effort`` threads into the metered runner the gating lanes build (#2192).

The single-trial path threads the resolved ``--effort`` / ``METERED_DEFAULT_EFFORT``
into ``make_runner(... effort=...)``; the always-metered pass@k (``--trials k>1``,
the CI gate) and model-matrix lanes must do the SAME. They now build through that
same ``make_runner`` chokepoint (so API-key resolution can never be bypassed),
and so must pass the lane effort, or the calibration never reaches the metered gate.
A scenario's own ``model@effort`` still wins at the runner's per-scenario seam; the
lane effort is the default for scenarios that declare none.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.cli.eval.multi_trial import run_model_matrix_lane, run_pass_at_k_lane
from teatree.eval.models import EvalRun, EvalSpec
from teatree.llm.credentials import AnthropicSubscriptionCredential


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


class _RecordingRunner:
    """Stand-in for ``ApiInProcessRunner`` that records its constructor effort."""

    last_effort: str | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_effort = kwargs.get("effort")

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


@pytest.fixture
def recording_runner() -> Iterator[type[_RecordingRunner]]:
    # Bypass the eval-credential resolver (which reads the settings + DB) to the
    # default subscription credential, keeping the lane exercised DB-free; the
    # credential-KIND selection has its own tests.
    _RecordingRunner.last_effort = None
    with (
        patch("teatree.credential_config.resolve_eval_credential", lambda **_: AnthropicSubscriptionCredential()),
        patch.object(AnthropicSubscriptionCredential, "export", return_value="oauth-test"),
        patch("teatree.eval.backends.ApiInProcessRunner", _RecordingRunner),
    ):
        yield _RecordingRunner


class TestPassAtKLaneThreadsEffort:
    def test_pass_at_k_lane_passes_the_lane_effort_into_the_runner(
        self, recording_runner: type[_RecordingRunner]
    ) -> None:
        run_pass_at_k_lane(
            [_spec()],
            max_turns=None,
            trials=3,
            require="any",
            output_format="json",
            effort="high",
        )
        assert recording_runner.last_effort == "high"


class TestMatrixLaneThreadsEffort:
    def test_matrix_lane_passes_the_lane_effort_into_the_runner(self, recording_runner: type[_RecordingRunner]) -> None:
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
            effort="high",
        )
        assert recording_runner.last_effort == "high"
