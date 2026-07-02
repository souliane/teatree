"""Tests for per-phase model tiering by ABSTRACT TIER (#880, #562 §3).

Every phase resolves to a concrete model id via phase → tier → model, with the
single :data:`TIER_MODELS` constant the only place a concrete id lives. These
tests assert via TIERS and the constant — never a concrete model-id string
literal — so adopting a new model needs zero test edits.
"""

from pathlib import Path

import pytest

import teatree.agents.model_tiering as mt_mod
from teatree.agents.model_tiering import (
    DEFAULT_PHASE_MODELS,
    DEFAULT_TIER,
    TIER_EFFORT,
    TIER_MODELS,
    model_supports_thinking,
    resolve_phase_model,
    resolve_spawn_effort,
    resolve_spawn_model,
    resolve_tier,
    resolve_tier_effort,
)

_ABSENT = Path("/nonexistent.toml")


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


class TestTierConstantIsSingleSource:
    """:data:`TIER_MODELS` is the only place concrete model ids live."""

    def test_three_named_tiers(self) -> None:
        assert set(TIER_MODELS) == {"frontier", "balanced", "cheap"}

    def test_resolve_tier_reads_the_constant(self) -> None:
        for tier, model in TIER_MODELS.items():
            assert resolve_tier(tier, config_path=_ABSENT) == model

    def test_unknown_tier_passes_through(self) -> None:
        # A concrete model id passed where a tier is expected is returned as-is —
        # the resolver never swallows a genuine id (or surfaces a typo downstream).
        assert resolve_tier(TIER_MODELS["frontier"], config_path=_ABSENT) == TIER_MODELS["frontier"]

    def test_default_tier_is_balanced(self) -> None:
        assert DEFAULT_TIER == "balanced"

    def test_config_overrides_a_tier(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_models]\nfrontier = "sentinel-frontier-model"\n')
        assert resolve_tier("frontier", config_path=cfg) == "sentinel-frontier-model"
        # An un-overridden tier still reads the shipped default.
        assert resolve_tier("balanced", config_path=cfg) == TIER_MODELS["balanced"]


class TestDefaultPhaseTiers:
    """Each phase maps to a tier; resolution returns that tier's concrete model."""

    def test_phase_tier_map_is_the_redesigned_set(self) -> None:
        assert DEFAULT_PHASE_MODELS == {
            "planning": "frontier",
            "coding": "frontier",
            "debugging": "frontier",
            "reviewing": "frontier",
            "retrospecting": "frontier",
            "testing": "balanced",
            "shipping": "balanced",
            "requesting_review": "cheap",
        }

    @pytest.mark.parametrize("phase", ["planning", "coding", "debugging", "reviewing", "retrospecting"])
    def test_frontier_phases_resolve_to_frontier_model(self, phase: str) -> None:
        assert resolve_phase_model(phase, config_path=_ABSENT) == TIER_MODELS["frontier"]

    @pytest.mark.parametrize("phase", ["testing", "shipping"])
    def test_balanced_phases_resolve_to_balanced_model(self, phase: str) -> None:
        assert resolve_phase_model(phase, config_path=_ABSENT) == TIER_MODELS["balanced"]

    def test_requesting_review_resolves_to_cheap_model(self) -> None:
        assert resolve_phase_model("requesting_review", config_path=_ABSENT) == TIER_MODELS["cheap"]

    def test_unknown_phase_resolves_to_default_tier(self) -> None:
        # An unmapped phase (e.g. scoping) falls back to DEFAULT_TIER (balanced).
        assert resolve_phase_model("scoping", config_path=_ABSENT) == TIER_MODELS[DEFAULT_TIER]


class TestPhaseModelOverrides:
    def test_override_to_a_tier(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.reviewing = "cheap"\n')
        assert resolve_phase_model("reviewing", config_path=cfg) == TIER_MODELS["cheap"]

    def test_override_to_a_concrete_model_id_passes_through(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.coding = "some-pinned-model-id"\n')
        assert resolve_phase_model("coding", config_path=cfg) == "some-pinned-model-id"

    def test_override_honours_tier_models_override(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.testing = "frontier"\n[agent.tier_models]\nfrontier = "sentinel-x"\n',
        )
        assert resolve_phase_model("testing", config_path=cfg) == "sentinel-x"

    @pytest.mark.parametrize("bogus", ["", "   ", "default", "inherit"])
    def test_sentinel_override_inherits(self, tmp_path: Path, bogus: str) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, f'[agent]\nphase_models.testing = "{bogus}"\n')
        assert resolve_phase_model("testing", config_path=cfg) is None


class TestMalformedAndMissing:
    def test_missing_agent_section_falls_back_to_tier_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[teatree]\nmode = "interactive"\n')
        assert resolve_phase_model("retrospecting", config_path=cfg) == TIER_MODELS["frontier"]
        assert resolve_phase_model("requesting_review", config_path=cfg) == TIER_MODELS["cheap"]

    def test_malformed_toml_falls_back_to_tier_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, "[agent\nphase_models.testing = not valid toml")
        assert resolve_phase_model("testing", config_path=cfg) == TIER_MODELS["balanced"]

    def test_non_table_phase_models_falls_back_to_tier_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models = "oops"\n')
        assert resolve_phase_model("retrospecting", config_path=cfg) == TIER_MODELS["frontier"]

    def test_default_config_path_used_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.shipping = "frontier"\n')
        monkeypatch.setattr(mt_mod, "CONFIG_PATH", cfg)
        import teatree.config_agent as ca_mod  # noqa: PLC0415

        monkeypatch.setattr(ca_mod, "CONFIG_PATH", cfg)
        assert resolve_phase_model("shipping") == TIER_MODELS["frontier"]


class TestSingleSourceProof:
    """Overriding TIER_MODELS["frontier"] flows to BOTH production and eval.

    The CORE proof: a single ``[agent.tier_models]`` override changes the model a
    planning-phase production spawn AND a frontier-tier eval resolution land on —
    no concrete id anywhere else.
    """

    _SENTINEL = "sentinel-frontier-9"

    def _cfg(self, tmp_path: Path) -> Path:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, f'[agent.tier_models]\nfrontier = "{self._SENTINEL}"\n')
        return cfg

    def test_production_planning_spawn_follows_override(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path)
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) == self._SENTINEL

    def test_frontier_tier_resolution_follows_override(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path)
        assert resolve_tier("frontier", config_path=cfg) == self._SENTINEL

    def test_mutation_check_indirection_not_bypassed(self, tmp_path: Path) -> None:
        # If resolution bypassed resolve_tier (hard-coded the model id), the
        # override would NOT take effect and the result would equal the shipped
        # default. Assert it does NOT — proving the indirection is live.
        cfg = self._cfg(tmp_path)
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) != TIER_MODELS["frontier"]
        assert resolve_phase_model("planning", config_path=cfg) != TIER_MODELS["frontier"]


class TestResolveSpawnModel:
    """`resolve_spawn_model(phase, *, skills)` — most-capable-wins floor merge.

    The phase model (concrete id) merged with the per-skill
    `[agent.skill_models]` floors. A floor only RAISES capability (tier-ranked,
    order-independent) and is compared in tier space.
    """

    def test_no_skill_floors_equals_phase_model(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.reviewing = "balanced"\n')
        assert resolve_spawn_model("reviewing", skills=[], config_path=cfg) == TIER_MODELS["balanced"]

    def test_absent_config_equals_phase_model_default(self) -> None:
        for phase in ("reviewing", "testing", "shipping", "retrospecting", "planning", "requesting_review"):
            assert resolve_spawn_model(phase, skills=["code-review"], config_path=_ABSENT) == resolve_phase_model(
                phase, config_path=_ABSENT
            )

    def test_skill_floor_raises_above_phase_model(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.requesting_review = "cheap"\n[agent.skill_models]\ncode-review = "frontier"\n',
        )
        # cheap phase floor + a frontier skill floor → frontier model (most capable wins).
        assert (
            resolve_spawn_model("requesting_review", skills=["code-review"], config_path=cfg) == TIER_MODELS["frontier"]
        )

    def test_skill_floor_below_phase_does_not_downgrade(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.planning = "frontier"\n[agent.skill_models]\ncode-review = "cheap"\n',
        )
        # A weaker skill floor never downgrades the stronger phase model.
        assert resolve_spawn_model("planning", skills=["code-review"], config_path=cfg) == TIER_MODELS["frontier"]

    def test_floor_merge_is_order_independent(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.requesting_review = "cheap"\n'
            '[agent.skill_models]\na = "cheap"\nb = "frontier"\nc = "balanced"\n',
        )
        # Most-capable floor wins regardless of skill order.
        assert (
            resolve_spawn_model("requesting_review", skills=["a", "b", "c"], config_path=cfg) == TIER_MODELS["frontier"]
        )
        assert (
            resolve_spawn_model("requesting_review", skills=["c", "b", "a"], config_path=cfg) == TIER_MODELS["frontier"]
        )

    def test_skill_not_in_skill_models_contributes_nothing(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg, '[agent]\nphase_models.requesting_review = "cheap"\n[agent.skill_models]\ncode-review = "frontier"\n'
        )
        # A loaded skill with no floor entry does not raise capability.
        assert (
            resolve_spawn_model("requesting_review", skills=["unlisted-skill"], config_path=cfg) == TIER_MODELS["cheap"]
        )

    def test_sentinel_skill_floor_contributes_nothing(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.requesting_review = "cheap"\n[agent.skill_models]\ncode-review = "inherit"\n',
        )
        # An inherit-sentinel floor is a no-op; the phase model stands.
        assert resolve_spawn_model("requesting_review", skills=["code-review"], config_path=cfg) == TIER_MODELS["cheap"]

    def test_default_config_path_used_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg, '[agent]\nphase_models.requesting_review = "cheap"\n[agent.skill_models]\ncode-review = "frontier"\n'
        )
        monkeypatch.setattr(mt_mod, "CONFIG_PATH", cfg)
        import teatree.config_agent as ca_mod  # noqa: PLC0415

        monkeypatch.setattr(ca_mod, "CONFIG_PATH", cfg)
        assert resolve_spawn_model("requesting_review", skills=["code-review"]) == TIER_MODELS["frontier"]


class TestNoFableDefault:
    """Pinning test (#2237 removal): nothing routes to Fable without explicit opt-in.

    The standalone ``fable_enabled`` kill-switch is gone — the safety property it
    protected is now structural: :data:`TIER_MODELS` never NAMES a Fable model id
    for any tier, so a phase can only ever reach Fable via an EXPLICIT
    ``[agent.tier_models]`` / ``[agent.skill_models]`` / ``[agent] honesty_model``
    override the operator writes themselves — never a shipped default.
    """

    def test_tier_models_never_names_a_fable_model_id(self) -> None:
        assert all("fable" not in model.lower() for model in TIER_MODELS.values())

    def test_default_phase_models_never_resolve_to_fable(self) -> None:
        for phase in DEFAULT_PHASE_MODELS:
            resolved = resolve_phase_model(phase, config_path=_ABSENT)
            assert resolved is not None
            assert "fable" not in resolved.lower()

    def test_absent_config_spawn_model_never_defaults_to_fable(self) -> None:
        for phase in (*DEFAULT_PHASE_MODELS, "scoping"):
            resolved = resolve_spawn_model(phase, skills=[], config_path=_ABSENT)
            assert resolved is not None
            assert "fable" not in resolved.lower()

    def test_default_honesty_model_is_opus_not_fable(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        assert AgentConfig().honesty_model == "opus"


class TestModelSupportsThinking:
    """The adaptive-thinking guard: reasoning tiers yes, cheap/Haiku and inherit no."""

    def test_frontier_model_supports_thinking(self) -> None:
        assert model_supports_thinking(TIER_MODELS["frontier"]) is True

    def test_balanced_model_supports_thinking(self) -> None:
        assert model_supports_thinking(TIER_MODELS["balanced"]) is True

    def test_cheap_haiku_model_does_not_support_thinking(self) -> None:
        # Haiku rejects the thinking/effort levers, so the guard withholds the pin.
        assert model_supports_thinking(TIER_MODELS["cheap"]) is False

    def test_unrecognised_model_supports_thinking(self) -> None:
        # An unrecognised id falls back to the conservative reasoning tier
        # (opus), which supports thinking — only the cheap/Haiku tier withholds it.
        assert model_supports_thinking("claude-some-future-model-9") is True

    def test_inherit_default_is_left_alone(self) -> None:
        # None = inherit the user's default: unknown model, so leave the SDK default.
        assert model_supports_thinking(None) is False
        assert model_supports_thinking("") is False


class TestTierEffortConstantIsSingleSource:
    """:data:`TIER_EFFORT` is the only place per-tier reasoning effort lives."""

    def test_only_reasoning_tiers_carry_effort(self) -> None:
        # frontier + balanced carry an effort; cheap (Haiku) is deliberately absent
        # so it inherits the SDK default (Haiku rejects the effort lever).
        assert TIER_EFFORT == {"frontier": "xhigh", "balanced": "high"}

    def test_resolve_tier_effort_reads_the_constant(self) -> None:
        for tier, effort in TIER_EFFORT.items():
            assert resolve_tier_effort(tier, config_path=_ABSENT) == effort

    def test_cheap_tier_has_no_effort(self) -> None:
        # A tier absent from TIER_EFFORT resolves to None (pin no --effort).
        assert resolve_tier_effort("cheap", config_path=_ABSENT) is None

    def test_unknown_tier_has_no_effort(self) -> None:
        # Unlike resolve_tier (which passes an id through), an unknown tier here is
        # None — a concrete model id is not a known effort tier, so emit no effort.
        assert resolve_tier_effort(TIER_MODELS["frontier"], config_path=_ABSENT) is None

    def test_config_overrides_a_tier_effort(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nbalanced = "max"\n')
        assert resolve_tier_effort("balanced", config_path=cfg) == "max"
        # An un-overridden tier still reads the shipped default.
        assert resolve_tier_effort("frontier", config_path=cfg) == TIER_EFFORT["frontier"]

    def test_invalid_override_value_dropped_falls_back_to_default(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nfrontier = "bogus"\n')
        # An off-scale override is dropped at parse, so the shipped default stands.
        assert resolve_tier_effort("frontier", config_path=cfg) == TIER_EFFORT["frontier"]


class TestResolveSpawnEffort:
    """`resolve_spawn_effort(phase)` — phase → tier → effort, mirroring resolve_phase_model."""

    @pytest.mark.parametrize("phase", ["planning", "coding", "debugging", "reviewing", "retrospecting"])
    def test_frontier_phases_resolve_to_frontier_effort(self, phase: str) -> None:
        assert resolve_spawn_effort(phase, config_path=_ABSENT) == TIER_EFFORT["frontier"]

    @pytest.mark.parametrize("phase", ["testing", "shipping"])
    def test_balanced_phases_resolve_to_balanced_effort(self, phase: str) -> None:
        assert resolve_spawn_effort(phase, config_path=_ABSENT) == TIER_EFFORT["balanced"]

    def test_cheap_phase_has_no_effort(self) -> None:
        # requesting_review is the cheap/Haiku tier — no effort pin.
        assert resolve_spawn_effort("requesting_review", config_path=_ABSENT) is None

    def test_unknown_phase_uses_default_tier_effort(self) -> None:
        # An unmapped phase falls back to DEFAULT_TIER (balanced) for effort too.
        assert resolve_spawn_effort("scoping", config_path=_ABSENT) == TIER_EFFORT[DEFAULT_TIER]

    def test_phase_models_override_lowers_effort_in_lockstep(self, tmp_path: Path) -> None:
        # Opting a frontier phase down to the cheap tier drops its effort with the
        # model — the same phase_models override drives both.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.reviewing = "cheap"\n')
        assert resolve_spawn_effort("reviewing", config_path=cfg) is None

    def test_phase_models_override_raises_effort(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.requesting_review = "frontier"\n')
        assert resolve_spawn_effort("requesting_review", config_path=cfg) == TIER_EFFORT["frontier"]

    def test_concrete_model_id_override_has_no_effort(self, tmp_path: Path) -> None:
        # A phase pinned to a concrete model id (not a tier) is not a known effort
        # tier, so no effort is pinned.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.coding = "some-pinned-model-id"\n')
        assert resolve_spawn_effort("coding", config_path=cfg) is None

    @pytest.mark.parametrize("sentinel", ["", "   ", "default", "inherit"])
    def test_sentinel_override_has_no_effort(self, tmp_path: Path, sentinel: str) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, f'[agent]\nphase_models.testing = "{sentinel}"\n')
        assert resolve_spawn_effort("testing", config_path=cfg) is None

    def test_tier_effort_override_flows_through_phase(self, tmp_path: Path) -> None:
        # The single-source proof for effort: a [agent.tier_effort] override changes
        # the effort a phase resolves to, with no per-phase effort literal anywhere.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nbalanced = "max"\n')
        assert resolve_spawn_effort("testing", config_path=cfg) == "max"

    def test_default_config_path_used_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nbalanced = "max"\n')
        monkeypatch.setattr(mt_mod, "CONFIG_PATH", cfg)
        import teatree.config_agent as ca_mod  # noqa: PLC0415

        monkeypatch.setattr(ca_mod, "CONFIG_PATH", cfg)
        assert resolve_spawn_effort("testing") == "max"
