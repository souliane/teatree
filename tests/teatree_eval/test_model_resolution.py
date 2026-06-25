"""Resolution precedence for an eval scenario's model: model > tier > phase > default.

Asserts via TIERS and the :data:`TIER_MODELS` constant — never a concrete
model-id literal — so adopting a new model needs zero test edits here.
"""

from pathlib import Path

from teatree.agents.model_tiering import DEFAULT_TIER, TIER_MODELS
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.models import EvalSpec

_ABSENT = Path("/nonexistent.toml")


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


class TestResolveEvalModel:
    def test_explicit_model_wins_over_everything(self) -> None:
        spec = _spec(model="claude-pinned-id@xhigh", tier="cheap", phase="planning")
        assert resolve_eval_model(spec, config_path=_ABSENT) == "claude-pinned-id@xhigh"

    def test_tier_resolves_through_the_constant(self) -> None:
        for tier, model in TIER_MODELS.items():
            assert resolve_eval_model(_spec(tier=tier), config_path=_ABSENT) == model

    def test_tier_wins_over_phase(self) -> None:
        # tier=cheap beats phase=planning (which would be frontier).
        spec = _spec(tier="cheap", phase="planning")
        assert resolve_eval_model(spec, config_path=_ABSENT) == TIER_MODELS["cheap"]

    def test_phase_resolves_via_its_default_tier(self) -> None:
        # planning -> frontier; testing -> balanced; requesting_review -> cheap.
        assert resolve_eval_model(_spec(phase="planning"), config_path=_ABSENT) == TIER_MODELS["frontier"]
        assert resolve_eval_model(_spec(phase="testing"), config_path=_ABSENT) == TIER_MODELS["balanced"]
        assert resolve_eval_model(_spec(phase="requesting_review"), config_path=_ABSENT) == TIER_MODELS["cheap"]

    def test_unmapped_phase_falls_back_to_default_tier(self) -> None:
        assert resolve_eval_model(_spec(phase="scoping"), config_path=_ABSENT) == TIER_MODELS[DEFAULT_TIER]

    def test_nothing_declared_is_default_tier(self) -> None:
        assert resolve_eval_model(_spec(), config_path=_ABSENT) == TIER_MODELS[DEFAULT_TIER]

    def test_tier_honours_config_override(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[agent.tier_models]\nfrontier = "sentinel-x"\n', encoding="utf-8")
        assert resolve_eval_model(_spec(tier="frontier"), config_path=cfg) == "sentinel-x"

    def test_phase_honours_config_override(self, tmp_path: Path) -> None:
        # A single tier override flows to a phase-based scenario too (planning →
        # frontier → the overridden id), proving the single-source indirection.
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[agent.tier_models]\nfrontier = "sentinel-x"\n', encoding="utf-8")
        assert resolve_eval_model(_spec(phase="planning"), config_path=cfg) == "sentinel-x"
