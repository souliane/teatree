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
    TIER_MODELS,
    resolve_phase_model,
    resolve_spawn_model,
    resolve_tier,
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


# The Fable-pinned surfaces (phase_models + skill_models + session_model) one
# config carries, used to prove the kill-switch downgrades EVERY surface, not a
# sampled subset.
_FABLE_PHASE_PINS = (
    'phase_models.planning = "fable"\n'
    'phase_models.coding = "fable"\n'
    'phase_models.debugging = "fable"\n'
    'phase_models.reviewing = "fable"\n'
    'phase_models.architectural_review = "fable"\n'
)
_FABLE_SKILL_PINS = (
    "[agent.skill_models]\n"
    'code-review = "fable"\n'
    'architecture-design = "fable"\n'
    't3-e2e = "claude-fable-5"\n'  # full id form, not the short alias
)

_ALL_PHASES = (
    "planning",
    "coding",
    "debugging",
    "reviewing",
    "requesting_review",
    "testing",
    "shipping",
    "retrospecting",
    "architectural_review",
    "scoping",
)

_SKILL_BUNDLES = (
    [],
    ["code-review"],
    ["architecture-design"],
    ["t3-e2e"],
    ["code-review", "architecture-design", "t3-e2e"],
    ["unlisted-skill"],
)


def _fable_pinned_cfg(tmp_path: Path, *, agent_scalars: str = "") -> Path:
    """A Fable-pinned config: phase_models + skill_models + session_model = fable."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(
        cfg,
        "[agent]\n" + 'session_model = "fable"\n' + agent_scalars + _FABLE_PHASE_PINS + _FABLE_SKILL_PINS,
    )
    return cfg


class TestFableKillSwitch:
    """``[agent] fable_enabled`` single-toggle downgrade (teatree#2237).

    When disabled, every resolved model value that is Fable (short ``fable`` or
    full ``claude-fable-5``) transparently downgrades to ``fable_fallback``
    (default ``opus`` = the frontier family) across every spawn + the session pin.
    """

    def test_disabled_downgrades_every_phase_skill_combo_to_fallback(self, tmp_path: Path) -> None:
        from teatree.core.cost import tier_of_model  # noqa: PLC0415

        on_dir = tmp_path / "on"
        on_dir.mkdir()
        off = _fable_pinned_cfg(tmp_path, agent_scalars="fable_enabled = false\n")
        on = _fable_pinned_cfg(on_dir, agent_scalars="fable_enabled = true\n")
        any_was_fable = False
        for phase in _ALL_PHASES:
            for bundle in _SKILL_BUNDLES:
                on_resolved = resolve_spawn_model(phase, skills=bundle, config_path=on)
                off_resolved = resolve_spawn_model(phase, skills=bundle, config_path=off)
                assert off_resolved != "fable", (phase, bundle, off_resolved)
                assert off_resolved != "claude-fable-5", (phase, bundle, off_resolved)
                if on_resolved is not None and tier_of_model(on_resolved) == "fable":
                    any_was_fable = True
                    # Every combo that resolved to Fable when ON now resolves to
                    # the fallback (default "opus") when OFF.
                    assert off_resolved == "opus", (phase, bundle, off_resolved)
                else:
                    assert off_resolved == on_resolved, (phase, bundle, on_resolved, off_resolved)
        assert any_was_fable, "fixture must exercise at least one Fable resolution"

    def test_enabled_is_byte_identical_to_today(self, tmp_path: Path) -> None:
        cfg = _fable_pinned_cfg(tmp_path, agent_scalars="fable_enabled = true\n")
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) == "fable"
        assert resolve_spawn_model("coding", skills=["code-review"], config_path=cfg) == "fable"
        # t3-e2e's floor is the full claude-fable-5 id, preserved byte-for-byte.
        assert resolve_spawn_model("testing", skills=["t3-e2e"], config_path=cfg) == "claude-fable-5"

    def test_absent_toggle_is_enabled_keeps_fable(self, tmp_path: Path) -> None:
        cfg = _fable_pinned_cfg(tmp_path)
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) == "fable"
        assert resolve_spawn_model("coding", skills=["architecture-design"], config_path=cfg) == "fable"

    def test_fable_fallback_override_to_balanced_tier(self, tmp_path: Path) -> None:
        cfg = _fable_pinned_cfg(tmp_path, agent_scalars='fable_enabled = false\nfable_fallback = "balanced"\n')
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) == "balanced"
        assert resolve_spawn_model("coding", skills=["code-review"], config_path=cfg) == "balanced"

    def test_non_fable_pins_untouched_when_disabled(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            "[agent]\nfable_enabled = false\n"
            'phase_models.reviewing = "balanced"\nphase_models.retrospecting = "cheap"\n',
        )
        assert resolve_spawn_model("reviewing", skills=[], config_path=cfg) == TIER_MODELS["balanced"]
        assert resolve_spawn_model("retrospecting", skills=[], config_path=cfg) == TIER_MODELS["cheap"]


class TestDowngradeFableHelper:
    """The pure ``_downgrade_fable(model, config)`` helper (teatree#2237)."""

    def test_short_alias_downgrades_when_disabled(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=False, fable_fallback="opus")
        assert mt_mod._downgrade_fable("fable", cfg) == "opus"

    def test_full_id_downgrades_when_disabled(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=False, fable_fallback="opus")
        assert mt_mod._downgrade_fable("claude-fable-5", cfg) == "opus"

    def test_left_unchanged_when_enabled(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=True, fable_fallback="opus")
        assert mt_mod._downgrade_fable("fable", cfg) == "fable"
        assert mt_mod._downgrade_fable("claude-fable-5", cfg) == "claude-fable-5"

    def test_non_fable_unchanged_when_disabled(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=False, fable_fallback="opus")
        assert mt_mod._downgrade_fable(TIER_MODELS["balanced"], cfg) == TIER_MODELS["balanced"]
        assert mt_mod._downgrade_fable(TIER_MODELS["frontier"], cfg) == TIER_MODELS["frontier"]

    def test_none_unchanged_when_disabled(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=False, fable_fallback="opus")
        assert mt_mod._downgrade_fable(None, cfg) is None

    def test_fallback_override_respected(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=False, fable_fallback="balanced")
        assert mt_mod._downgrade_fable("fable", cfg) == "balanced"
