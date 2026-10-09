"""The pass@k and matrix lanes RUN on the backend the caller asked for, and guard it.

``require_api_backend_for_fresh_run`` admits either fresh Claude backend for
``--trials``/``--models``, so ``--backend anthropic_api --trials 3`` is an accepted
shape — the CLI-free lane CI runs on. Both lanes nonetheless built
``make_runner(API_BACKEND, ...)`` unconditionally, so that request silently executed
on the ``claude``-CLI child the lane exists to avoid; where no CLI is provisioned
every trial then skips. Same class as the escalation runner's fix one module over.

The pass@k lane's vacuous-green guard was the ``$0``-cost one, which keys on
``cost_usd`` and is structurally blind to every unmetered fresh backend — so once
the backend is honoured the empty-trajectory guard has to run beside it, or an
``anthropic_api`` sweep that drove nothing still reports green.
"""

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from teatree.cli.eval.multi_trial import run_model_matrix_lane, run_pass_at_k_lane
from teatree.eval.backends import ANTHROPIC_API_BACKEND, API_BACKEND
from teatree.eval.models import EvalRun, EvalSpec


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


def _run(spec: EvalSpec, *, empty: bool) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=() if empty else ("did the thing",),
        terminal_reason="end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        # An empty trajectory bills nothing, which is exactly why the $0 guard cannot
        # tell it apart from a healthy unmetered lane.
        cost_usd=0.0 if empty else 0.02,
    )


class _StubRunner:
    def __init__(self, *, empty: bool) -> None:
        self._empty = empty

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec, empty=self._empty)


@dataclasses.dataclass
class _Lane:
    """The backends the lane asked ``make_runner`` for, and what its runs produce."""

    backends: list[str] = dataclasses.field(default_factory=list)
    empty_trajectory: bool = False


@pytest.fixture
def lane() -> Iterator[_Lane]:
    recorded = _Lane()

    def _make_runner(backend: str, *_: object, **__: object) -> _StubRunner:
        recorded.backends.append(backend)
        return _StubRunner(empty=recorded.empty_trajectory)

    with patch("teatree.cli.eval.multi_trial.make_runner", _make_runner):
        yield recorded


class TestLanesRunOnTheRequestedBackend:
    def test_pass_at_k_lane_builds_the_runner_for_the_callers_backend(self, lane: _Lane) -> None:
        run_pass_at_k_lane(
            [_spec()],
            backend=ANTHROPIC_API_BACKEND,
            max_turns=None,
            trials=3,
            require="any",
            output_format="json",
        )
        assert lane.backends == [ANTHROPIC_API_BACKEND]

    def test_matrix_lane_builds_the_runner_for_the_callers_backend(self, lane: _Lane) -> None:
        run_model_matrix_lane(
            [_spec()],
            backend=ANTHROPIC_API_BACKEND,
            models="claude-opus-4-8",
            max_turns=None,
            trials=1,
            require="any",
            output_format="json",
            persist=False,
            baseline=False,
            gate_regressions=False,
        )
        assert lane.backends == [ANTHROPIC_API_BACKEND]

    def test_pass_at_k_lane_still_builds_the_api_runner_for_the_api_backend(self, lane: _Lane) -> None:
        run_pass_at_k_lane(
            [_spec()],
            backend=API_BACKEND,
            max_turns=None,
            trials=3,
            require="any",
            output_format="json",
        )
        assert lane.backends == [API_BACKEND]


class TestPassAtKLaneGuardsTheUnmeteredFreshBackend:
    def test_an_all_empty_anthropic_api_sweep_reds_the_lane(self, lane: _Lane) -> None:
        lane.empty_trajectory = True
        with pytest.raises(typer.Exit) as exit_info:
            run_pass_at_k_lane(
                [_spec()],
                backend=ANTHROPIC_API_BACKEND,
                max_turns=None,
                trials=2,
                require="any",
                output_format="json",
            )
        assert exit_info.value.exit_code == 1

    def test_a_producing_anthropic_api_sweep_passes(self, lane: _Lane) -> None:
        run_pass_at_k_lane(
            [_spec()],
            backend=ANTHROPIC_API_BACKEND,
            max_turns=None,
            trials=2,
            require="any",
            output_format="json",
        )
