"""A presetless ``t3 eval run`` dispatch resolves byte-identically to ``resolve_eval_model``.

Pins that nobody makes a preset the implicit default — presets are a SEPARATE
composition layer (`teatree.eval.presets`) applied only when ``--preset`` is
explicitly given (``ResolvedRun.preset is not None``). With no preset active,
the per-scenario seam (``run_dispatch._resolve_per_scenario_model``) must
thread every scenario through the SAME ``resolve_eval_model`` the guardrail in
``test_catalog_never_resolves_frontier.py`` pins — never a preset default, and
never any other resolution.
"""

from pathlib import Path

from teatree.cli.eval.run_dispatch import _resolve_per_scenario_model
from teatree.eval.discovery import discover_specs
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.models import EvalSpec


def _spec(*, model: str = "", tier: str = "", phase: str = "") -> EvalSpec:
    return EvalSpec(
        name="s",
        scenario="sc",
        agent_path="skills/code/SKILL.md",
        prompt="p",
        matchers=(),
        source_path=Path("evals/scenarios/x.yaml"),
        model=model,
        tier=tier,
        phase=phase,
    )


class TestPresetlessDispatchMatchesResolveEvalModel:
    def test_every_shipped_scenario_resolves_identically(self) -> None:
        for spec in discover_specs():
            assert _resolve_per_scenario_model(spec, preset=None).model == resolve_eval_model(spec)

    def test_every_precedence_combination_resolves_identically(self) -> None:
        combos = (
            _spec(model="pinned@xhigh", tier="cheap", phase="planning"),
            _spec(tier="frontier", phase="planning"),
            _spec(tier="cheap"),
            _spec(phase="planning"),
            _spec(phase="testing"),
            _spec(phase="requesting_review"),
            _spec(phase="an_unmapped_phase"),
            _spec(),
        )
        for spec in combos:
            assert _resolve_per_scenario_model(spec, preset=None).model == resolve_eval_model(spec)
