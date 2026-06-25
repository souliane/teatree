"""``dispatch_resolved_run`` fans a resolved ``t3 eval run`` to its lane.

The command body resolves every CLI argument into a :class:`ResolvedRun`, then
this dispatch picks the lane: ``--models`` → the matrix, ``--trials k>1`` → the
pass@k sweep, else the single-trial path (the only one carrying the
``--escalate-on-fail`` config). These pin which lane each shape routes to, against
stubbed lane functions so no live model runs.
"""

from pathlib import Path
from typing import Any

import pytest

from teatree.cli.eval.escalate import EscalationConfig
from teatree.cli.eval.run_dispatch import ResolvedRun, dispatch_resolved_run
from teatree.eval.models import EvalSpec


def _spec(name: str = "s") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
    )


def _resolved(**overrides: Any) -> ResolvedRun:
    base: dict[str, Any] = {
        "backend": "api",
        "max_turns": None,
        "transcript_dir": None,
        "require_executed": True,
        "max_budget_usd": 1.0,
        "effort": "medium",
        "parallel": 1,
        "output_format": "json",
        "judge": False,
        "transcript_html": None,
        "summary_md": None,
        "trials": 1,
        "require": "any",
        "models": None,
        "persist": False,
        "baseline": False,
        "gate_regressions": False,
        "gate_cost_regression": False,
    }
    base.update(overrides)
    return ResolvedRun(**base)


class _LaneSpy:
    def __init__(self) -> None:
        self.called = False
        self.kwargs: dict[str, Any] = {}

    def __call__(self, _specs: list[EvalSpec], **kwargs: Any) -> None:
        self.called = True
        self.kwargs = kwargs


@pytest.fixture
def lanes(monkeypatch: pytest.MonkeyPatch) -> dict[str, _LaneSpy]:
    spies = {name: _LaneSpy() for name in ("matrix", "pass_at_k", "single")}
    monkeypatch.setattr("teatree.cli.eval.run_dispatch.run_model_matrix_lane", spies["matrix"])
    monkeypatch.setattr("teatree.cli.eval.run_dispatch.run_pass_at_k_lane", spies["pass_at_k"])
    monkeypatch.setattr("teatree.cli.eval.run_dispatch.run_single_trial", spies["single"])
    return spies


class TestDispatchResolvedRun:
    def test_models_routes_to_the_matrix_lane(self, lanes: dict[str, _LaneSpy]) -> None:
        dispatch_resolved_run([_spec()], _resolved(models="opus,sonnet"), grader=None, escalation=None)
        assert lanes["matrix"].called
        assert not lanes["pass_at_k"].called
        assert not lanes["single"].called

    def test_trials_routes_to_the_pass_at_k_lane(self, lanes: dict[str, _LaneSpy]) -> None:
        dispatch_resolved_run([_spec()], _resolved(trials=3), grader=None, escalation=None)
        assert lanes["pass_at_k"].called
        assert not lanes["matrix"].called
        assert not lanes["single"].called

    def test_single_trial_is_the_default_lane(self, lanes: dict[str, _LaneSpy]) -> None:
        dispatch_resolved_run([_spec()], _resolved(), grader=None, escalation=None)
        assert lanes["single"].called
        assert not lanes["matrix"].called
        assert not lanes["pass_at_k"].called

    def test_escalation_is_threaded_only_to_the_single_trial_lane(self, lanes: dict[str, _LaneSpy]) -> None:
        config = EscalationConfig(escalate_trials=3)
        dispatch_resolved_run([_spec()], _resolved(), grader=None, escalation=config)
        assert lanes["single"].kwargs["escalation"] is config
