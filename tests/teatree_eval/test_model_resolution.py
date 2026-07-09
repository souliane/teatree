"""Resolution precedence for an eval scenario's model: model > tier > phase > default.

Asserts via TIERS and the :data:`TIER_MODELS` constant — never a concrete
model-id literal — so adopting a new model needs zero test edits here.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.agents.model_tiering import DEFAULT_TIER, TIER_MODELS
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


def _seed_tier_models(db_path: Path, overrides: dict[str, str]) -> None:
    """Plant an ``agent_tier_models`` row in a cold-reader config DB."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'agent_tier_models', ?)",
            (json.dumps(overrides),),
        )
        conn.commit()
    finally:
        conn.close()


class TestResolveEvalModel:
    def test_explicit_model_wins_over_everything(self) -> None:
        spec = _spec(model="claude-pinned-id@xhigh", tier="cheap", phase="planning")
        assert resolve_eval_model(spec) == "claude-pinned-id@xhigh"

    def test_tier_resolves_through_the_constant(self) -> None:
        for tier, model in TIER_MODELS.items():
            assert resolve_eval_model(_spec(tier=tier)) == model

    def test_tier_wins_over_phase(self) -> None:
        # tier=cheap beats phase=planning (which would be frontier).
        spec = _spec(tier="cheap", phase="planning")
        assert resolve_eval_model(spec) == TIER_MODELS["cheap"]

    def test_phase_resolves_via_its_default_tier(self) -> None:
        # planning -> frontier; testing -> balanced; requesting_review -> cheap.
        assert resolve_eval_model(_spec(phase="planning")) == TIER_MODELS["frontier"]
        assert resolve_eval_model(_spec(phase="testing")) == TIER_MODELS["balanced"]
        assert resolve_eval_model(_spec(phase="requesting_review")) == TIER_MODELS["cheap"]

    def test_unmapped_phase_falls_back_to_default_tier(self) -> None:
        assert resolve_eval_model(_spec(phase="scoping")) == TIER_MODELS[DEFAULT_TIER]

    def test_nothing_declared_is_default_tier(self) -> None:
        assert resolve_eval_model(_spec()) == TIER_MODELS[DEFAULT_TIER]

    def test_tier_honours_db_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "config.sqlite3"
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _seed_tier_models(db, {"frontier": "sentinel-x"})
        assert resolve_eval_model(_spec(tier="frontier")) == "sentinel-x"

    def test_phase_honours_db_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A single tier override flows to a phase-based scenario too (planning →
        # frontier → the overridden id), proving the single-source indirection.
        db = tmp_path / "config.sqlite3"
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        _seed_tier_models(db, {"frontier": "sentinel-x"})
        assert resolve_eval_model(_spec(phase="planning")) == "sentinel-x"
