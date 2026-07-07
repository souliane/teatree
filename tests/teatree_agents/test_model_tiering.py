"""Tests for per-phase model tiering by ABSTRACT TIER (#880, #562 §3).

Every phase resolves to a concrete model id via phase → tier → model, with the
single :data:`TIER_MODELS` constant the only place a concrete id lives. These
tests assert via TIERS and the constant — never a concrete model-id string
literal — so adopting a new model needs zero test edits.
"""

from pathlib import Path

import pytest
from django.test import TestCase

import teatree.agents.model_tiering as mt_mod
from teatree.agents.model_tiering import (
    DEFAULT_PHASE_MODELS,
    DEFAULT_TIER,
    HARNESS_EFFORT_SCALE,
    PHASE_HARNESS,
    PYDANTIC_AI_TIER_MODELS,
    TIER_EFFORT,
    TIER_MODELS,
    VERIFICATION_PHASES,
    _resolve_pydantic_ai_tier,
    assert_model_allowed_on_regulated_path,
    is_regulated_path_eligible,
    model_supports_thinking,
    resolve_phase_harness,
    resolve_phase_model,
    resolve_pydantic_ai_model,
    resolve_spawn_effort,
    resolve_spawn_model,
    resolve_tier,
    resolve_tier_effort,
)
from teatree.config import AgentHarness
from teatree.core.models import ConfigSetting

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


class TestHarnessScopedEffort:
    """Effort resolution is scoped to the ACTIVE harness (#2885).

    ``TIER_MODELS`` is harness-INDEPENDENT by design (both backends target the
    same concrete model catalog); ``TIER_EFFORT`` resolution is genuinely
    harness-scoped because the two harnesses' effort vocabularies differ
    (``claude_sdk`` has ``max``, ``pydantic_ai`` does not — see
    :data:`HARNESS_EFFORT_SCALE`).
    """

    def test_harness_effort_scale_has_an_entry_per_agent_harness(self) -> None:
        assert set(HARNESS_EFFORT_SCALE) == set(AgentHarness)

    def test_claude_sdk_scale_matches_the_shared_effort_scale(self) -> None:
        from teatree.config_agent import EFFORT_SCALE  # noqa: PLC0415

        assert HARNESS_EFFORT_SCALE[AgentHarness.CLAUDE_SDK] == EFFORT_SCALE

    def test_pydantic_ai_scale_has_no_max_rung(self) -> None:
        assert "max" not in HARNESS_EFFORT_SCALE[AgentHarness.PYDANTIC_AI]

    def test_shipped_defaults_are_valid_on_both_harnesses(self) -> None:
        # The no-op guarantee: the shipped xhigh/high values never get dropped
        # by the harness-scale check on either harness.
        for harness in AgentHarness:
            for tier, effort in TIER_EFFORT.items():
                assert resolve_tier_effort(tier, harness=harness, config_path=_ABSENT) == effort

    def test_claude_sdk_accepts_a_max_override(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nfrontier = "max"\n')
        assert resolve_tier_effort("frontier", harness=AgentHarness.CLAUDE_SDK, config_path=cfg) == "max"

    def test_pydantic_ai_drops_a_max_override_and_falls_back_to_default(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nfrontier = "max"\n')
        assert (
            resolve_tier_effort("frontier", harness=AgentHarness.PYDANTIC_AI, config_path=cfg)
            == TIER_EFFORT["frontier"]
        )

    def test_pydantic_ai_accepts_an_override_within_the_shared_vocabulary(self, tmp_path: Path) -> None:
        # "low" is in EFFORT_SCALE (so the config-time parser in config_agent.py
        # accepts it) AND in pydantic_ai's HARNESS_EFFORT_SCALE, so it passes
        # straight through — the harness-scale check only narrows, never widens
        # what an operator can already configure.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nbalanced = "low"\n')
        assert resolve_tier_effort("balanced", harness=AgentHarness.PYDANTIC_AI, config_path=cfg) == "low"

    def test_resolve_spawn_effort_threads_the_harness_through(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.tier_effort]\nfrontier = "max"\n')
        assert resolve_spawn_effort("planning", harness=AgentHarness.CLAUDE_SDK, config_path=cfg) == "max"
        assert (
            resolve_spawn_effort("planning", harness=AgentHarness.PYDANTIC_AI, config_path=cfg)
            == TIER_EFFORT["frontier"]
        )


class TestHarnessScopedEffortDefaultHarness(TestCase):
    """The default ``harness=None`` reads the DB-home ``agent_harness`` setting."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        self.cfg = tmp_path / ".teatree.toml"
        _write_toml(self.cfg, '[agent.tier_effort]\nfrontier = "max"\n')

    def test_defaults_to_the_resolved_agent_harness_setting(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        # No explicit harness= passed: resolves via get_effective_settings(),
        # which now reads the stored pydantic_ai setting — "max" is dropped.
        assert resolve_tier_effort("frontier", config_path=self.cfg) == TIER_EFFORT["frontier"]

        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        # Same override, claude_sdk harness: "max" is on-scale, passes through.
        assert resolve_tier_effort("frontier", config_path=self.cfg) == "max"


class TestIsRegulatedPathEligible:
    """:func:`is_regulated_path_eligible` — membership in the explicit allowlist, case-insensitively."""

    @pytest.mark.parametrize("model_id", ["anthropic/claude-opus-4.8", "Anthropic/Claude-Sonnet", "google/gemini-3"])
    def test_an_allowlisted_pattern_is_eligible(self, model_id: str) -> None:
        assert is_regulated_path_eligible(model_id, ["anthropic/", "google/"])

    @pytest.mark.parametrize("model_id", ["deepseek/deepseek-v4-pro", "qwen/qwen3.6-plus"])
    def test_a_model_off_the_allowlist_is_ineligible(self, model_id: str) -> None:
        assert not is_regulated_path_eligible(model_id, ["anthropic/", "google/"])

    def test_empty_allowlist_makes_nothing_eligible(self) -> None:
        assert not is_regulated_path_eligible("anthropic/claude-opus-4.8", [])


class TestAssertModelAllowedOnRegulatedPath:
    """:func:`assert_model_allowed_on_regulated_path` — the regulated-lane allowlist gate."""

    def test_unenforced_lane_never_raises(self) -> None:
        # The teatree factory lane carries no regulated data — any model runs.
        assert_model_allowed_on_regulated_path("deepseek/deepseek-v4-pro", enforce_regulated_path=False, allowlist=[])

    def test_allowlisted_model_on_the_regulated_path_is_a_noop(self) -> None:
        assert_model_allowed_on_regulated_path(
            "anthropic/claude-opus-4.8", enforce_regulated_path=True, allowlist=["anthropic/"]
        )

    def test_model_off_the_allowlist_is_refused_on_the_regulated_path(self) -> None:
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            assert_model_allowed_on_regulated_path(
                "deepseek/deepseek-v4-pro", enforce_regulated_path=True, allowlist=["anthropic/"]
            )

    def test_enforced_but_empty_allowlist_refuses_everything(self) -> None:
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            assert_model_allowed_on_regulated_path(
                "anthropic/claude-opus-4.8", enforce_regulated_path=True, allowlist=[]
            )


class TestAssertModelAllowedDefaultSettings(TestCase):
    """The default (params ``None``) reads the resolved DB-home regulated-path settings."""

    def test_default_unenforced_never_raises(self) -> None:
        # No row set — enforce_regulated_path defaults False, so nothing is gated.
        assert_model_allowed_on_regulated_path("deepseek/deepseek-v4-pro")

    def test_default_reads_the_resolved_regulated_path_settings(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            assert_model_allowed_on_regulated_path("deepseek/deepseek-v4-pro")

    def test_allowlisted_model_passes_under_enforcement(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/", "claude"])
        assert_model_allowed_on_regulated_path("anthropic/claude-opus-4.8")


class TestPydanticAiTierModels:
    """:data:`PYDANTIC_AI_TIER_MODELS` — the OrcaRouter catalog, SEPARATE from :data:`TIER_MODELS`."""

    def test_three_named_tiers_collapse_to_the_router_handle(self) -> None:
        # All abstract tiers point at ONE router handle — the router's own bandit
        # does the mundane-vs-hard tiering (OrcaRouter setup plan §3.3).
        assert set(PYDANTIC_AI_TIER_MODELS) == {"frontier", "balanced", "cheap"}
        assert set(PYDANTIC_AI_TIER_MODELS.values()) == {"orcarouter/teatree-factory"}

    def test_orca_catalog_never_carries_a_claude_dash_form_id(self) -> None:
        # The whole reason the table is forked: Orca does not carry the dash-form
        # Claude ids TIER_MODELS emits.
        for handle in PYDANTIC_AI_TIER_MODELS.values():
            assert "claude-" not in handle

    def test_resolve_pydantic_ai_tier_reads_the_constant(self) -> None:
        for tier, handle in PYDANTIC_AI_TIER_MODELS.items():
            assert _resolve_pydantic_ai_tier(tier, config_path=_ABSENT) == handle

    def test_unknown_tier_falls_back_to_the_default_handle(self) -> None:
        # NEVER passed through as a bare tier name — Orca would reject it.
        assert _resolve_pydantic_ai_tier("nonsense", config_path=_ABSENT) == PYDANTIC_AI_TIER_MODELS[DEFAULT_TIER]

    def test_config_overrides_a_pydantic_ai_tier(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.pydantic_ai_tier_models]\nfrontier = "orcarouter/other-router"\n')
        assert _resolve_pydantic_ai_tier("frontier", config_path=cfg) == "orcarouter/other-router"
        # An untouched tier keeps the shipped handle.
        assert _resolve_pydantic_ai_tier("balanced", config_path=cfg) == PYDANTIC_AI_TIER_MODELS["balanced"]

    def test_pydantic_ai_override_does_not_leak_into_claude_tier_models(self, tmp_path: Path) -> None:
        # The two catalogs are independent: overriding the OrcaRouter table never
        # touches the claude_sdk TIER_MODELS resolution.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.pydantic_ai_tier_models]\nfrontier = "orcarouter/other-router"\n')
        assert resolve_tier("frontier", config_path=cfg) == TIER_MODELS["frontier"]


class TestResolvePydanticAiModel:
    """:func:`resolve_pydantic_ai_model` — THE dash-form id normalisation (plan §3.2)."""

    @pytest.mark.parametrize("claude_id", list(TIER_MODELS.values()))
    def test_a_claude_dash_form_default_maps_to_the_router_handle(self, claude_id: str) -> None:
        # The bug: options.model is a teatree-abstract-tier default in Claude
        # dash-form, which OrcaRouter does not carry. It must NOT be sent verbatim.
        resolved = resolve_pydantic_ai_model(claude_id, config_path=_ABSENT)
        assert resolved == "orcarouter/teatree-factory"
        assert resolved != claude_id

    def test_none_maps_to_the_default_router_handle(self) -> None:
        assert resolve_pydantic_ai_model(None, config_path=_ABSENT) == PYDANTIC_AI_TIER_MODELS[DEFAULT_TIER]

    def test_each_claude_tier_maps_to_its_pydantic_tier_handle(self, tmp_path: Path) -> None:
        # Tier-faithful: a per-tier handle override is honoured because the Claude
        # id is normalised back to its abstract tier first.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent.pydantic_ai_tier_models]\nfrontier = "orcarouter/hard"\ncheap = "orcarouter/mundane"\n',
        )
        assert resolve_pydantic_ai_model(TIER_MODELS["frontier"], config_path=cfg) == "orcarouter/hard"
        assert resolve_pydantic_ai_model(TIER_MODELS["cheap"], config_path=cfg) == "orcarouter/mundane"

    @pytest.mark.parametrize(
        "orca_native_id",
        ["deepseek/deepseek-v4-pro", "anthropic/claude-opus-4.8", "orcarouter/teatree-factory", "qwen/qwen3.6-plus"],
    )
    def test_an_explicit_orca_native_pin_passes_through_unchanged(self, orca_native_id: str) -> None:
        # A provider-prefixed id is an explicit operator pin in Orca's own
        # namespace — never remapped to the router handle.
        assert resolve_pydantic_ai_model(orca_native_id, config_path=_ABSENT) == orca_native_id

    def test_router_name_override_replaces_the_default_handle(self) -> None:
        # The per-overlay router-name selection (secondary-router vs teatree-factory):
        # when the id normalises UP to a handle, the config/overlay override wins.
        assert (
            resolve_pydantic_ai_model(None, router_name="orcarouter/secondary-factory", config_path=_ABSENT)
            == "orcarouter/secondary-factory"
        )
        assert (
            resolve_pydantic_ai_model(
                "claude-opus-4-8", router_name="orcarouter/secondary-factory", config_path=_ABSENT
            )
            == "orcarouter/secondary-factory"
        )

    def test_router_name_override_does_not_touch_an_explicit_orca_native_pin(self) -> None:
        # An explicit provider-prefixed pin is authoritative — the overlay handle
        # override applies ONLY to the normalise-up branch.
        assert (
            resolve_pydantic_ai_model(
                "deepseek/deepseek-v4-pro", router_name="orcarouter/secondary-factory", config_path=_ABSENT
            )
            == "deepseek/deepseek-v4-pro"
        )

    def test_no_router_name_override_keeps_the_default_handle(self) -> None:
        assert resolve_pydantic_ai_model(None, router_name=None, config_path=_ABSENT) == "orcarouter/teatree-factory"


class TestResolvePhaseHarness:
    """:func:`resolve_phase_harness` — the cheap-model verifier pin (plan §4 guardrail #2)."""

    def test_verification_phases_are_pinned_to_claude_sdk(self) -> None:
        assert set(PHASE_HARNESS) == set(VERIFICATION_PHASES)
        assert set(PHASE_HARNESS.values()) == {AgentHarness.CLAUDE_SDK}

    @pytest.mark.parametrize("phase", sorted(VERIFICATION_PHASES))
    def test_a_verification_phase_forces_claude_sdk_even_when_pydantic_ai_configured(self, phase: str) -> None:
        # The MAKER may run a cheap open-source model via pydantic_ai; the checker stays on Claude.
        assert resolve_phase_harness(AgentHarness.PYDANTIC_AI, phase) is AgentHarness.CLAUDE_SDK

    @pytest.mark.parametrize("phase", ["coding", "planning", "debugging", "shipping"])
    def test_a_maker_phase_uses_the_configured_harness(self, phase: str) -> None:
        assert resolve_phase_harness(AgentHarness.PYDANTIC_AI, phase) is AgentHarness.PYDANTIC_AI
        assert resolve_phase_harness(AgentHarness.CLAUDE_SDK, phase) is AgentHarness.CLAUDE_SDK

    def test_absent_phase_uses_the_configured_harness(self) -> None:
        assert resolve_phase_harness(AgentHarness.PYDANTIC_AI, None) is AgentHarness.PYDANTIC_AI

    def test_a_verification_phase_never_overrides_a_claude_sdk_config(self) -> None:
        # The pin only ever forces claude_sdk — it never flips a maker onto pydantic.
        for phase in VERIFICATION_PHASES:
            assert resolve_phase_harness(AgentHarness.CLAUDE_SDK, phase) is AgentHarness.CLAUDE_SDK
