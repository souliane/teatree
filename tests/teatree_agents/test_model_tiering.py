"""Tests for per-phase headless model tiering (#880, #562 §3).

Mechanical phases resolve to a cheaper model tier; judgment phases keep
the user's default model. The mapping is config-driven via
``~/.teatree.toml [agent] phase_models.<phase>``.
"""

from pathlib import Path

import pytest

import teatree.agents.model_tiering as mt_mod
from teatree.agents.model_tiering import DEFAULT_PHASE_MODELS, resolve_phase_model, resolve_spawn_model


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_default_mechanical_phase_resolves_to_cheap_tier() -> None:
    """A mechanical phase (retrospecting) downgrades to haiku by default."""
    assert resolve_phase_model("retrospecting", config_path=Path("/nonexistent.toml")) == "haiku"


def test_default_review_test_ship_resolve_to_sonnet() -> None:
    """Review/test/ship are mechanical-ish: sonnet by default."""
    for phase in ("reviewing", "testing", "shipping"):
        assert resolve_phase_model(phase, config_path=Path("/nonexistent.toml")) == "sonnet"


def test_default_reasoning_phase_inherits_user_default() -> None:
    """A judgment phase (coding) returns None so no --model flag is added."""
    assert resolve_phase_model("coding", config_path=Path("/nonexistent.toml")) is None
    assert resolve_phase_model("debugging", config_path=Path("/nonexistent.toml")) is None


def test_unknown_phase_inherits_user_default() -> None:
    """An unmapped phase is conservative: keep the user's default model."""
    assert resolve_phase_model("scoping", config_path=Path("/nonexistent.toml")) is None


def test_config_overrides_default_mapping(tmp_path: Path) -> None:
    """``[agent] phase_models.reviewing = "opus"`` pins the reasoning model."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[agent]\nphase_models.reviewing = "opus"\n')
    assert resolve_phase_model("reviewing", config_path=cfg) == "opus"


def test_config_can_downgrade_a_reasoning_phase(tmp_path: Path) -> None:
    """Users may opt a reasoning phase into a cheaper tier explicitly."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[agent]\nphase_models.coding = "sonnet"\n')
    assert resolve_phase_model("coding", config_path=cfg) == "sonnet"


def test_config_empty_string_inherits_user_default(tmp_path: Path) -> None:
    """An explicit empty string disables tiering for that phase."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[agent]\nphase_models.reviewing = ""\n')
    assert resolve_phase_model("reviewing", config_path=cfg) is None


def test_default_mapping_constant_is_conservative() -> None:
    """The shipped default pins planning to opus and downgrades mechanical phases."""
    assert DEFAULT_PHASE_MODELS == {
        "planning": "opus",
        "reviewing": "sonnet",
        "requesting_review": "sonnet",
        "testing": "sonnet",
        "shipping": "sonnet",
        "retrospecting": "haiku",
    }
    for reasoning_phase in ("coding", "debugging"):
        assert reasoning_phase not in DEFAULT_PHASE_MODELS


def test_missing_agent_section_falls_back_to_defaults(tmp_path: Path) -> None:
    """A config without an ``[agent]`` section uses the shipped defaults."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[teatree]\nmode = "interactive"\n')
    assert resolve_phase_model("retrospecting", config_path=cfg) == "haiku"
    assert resolve_phase_model("coding", config_path=cfg) is None


@pytest.mark.parametrize("bogus", ["", "   ", "default", "inherit"])
def test_sentinel_values_inherit_user_default(tmp_path: Path, bogus: str) -> None:
    """Sentinel opt-out values disable the per-phase override."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, f'[agent]\nphase_models.testing = "{bogus}"\n')
    assert resolve_phase_model("testing", config_path=cfg) is None


def test_malformed_toml_falls_back_to_defaults(tmp_path: Path) -> None:
    """A syntactically broken config never crashes resolution."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, "[agent\nphase_models.testing = not valid toml")
    assert resolve_phase_model("testing", config_path=cfg) == "sonnet"
    assert resolve_phase_model("coding", config_path=cfg) is None


def test_non_table_phase_models_falls_back_to_defaults(tmp_path: Path) -> None:
    """``phase_models`` declared as a scalar (not a table) is ignored."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[agent]\nphase_models = "oops"\n')
    assert resolve_phase_model("retrospecting", config_path=cfg) == "haiku"


def test_planning_resolves_to_opus_by_default() -> None:
    """Planning is a genuine-design phase; the default tier pins it to opus."""
    assert resolve_phase_model("planning", config_path=Path("/nonexistent.toml")) == "opus"


def test_planning_overridable_to_inherit(tmp_path: Path) -> None:
    """An empty override opts planning out of tiering (inherits user default)."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[agent]\nphase_models.planning = ""\n')
    assert resolve_phase_model("planning", config_path=cfg) is None


def test_planning_overridable_to_sonnet(tmp_path: Path) -> None:
    """Config can downgrade planning to a cheaper tier if the user wants to."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[agent]\nphase_models.planning = "sonnet"\n')
    assert resolve_phase_model("planning", config_path=cfg) == "sonnet"


def test_requesting_review_resolves_to_sonnet_by_default() -> None:
    """Requesting-review is a mechanical handoff phase; sonnet suffices."""
    assert resolve_phase_model("requesting_review", config_path=Path("/nonexistent.toml")) == "sonnet"


def test_coding_still_inherits_user_default() -> None:
    """Coding stays unmapped (None) so the user's full-reasoning default applies."""
    assert resolve_phase_model("coding", config_path=Path("/nonexistent.toml")) is None


def test_default_config_path_used_when_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When no config_path is given, the canonical CONFIG_PATH is read."""
    cfg = tmp_path / ".teatree.toml"
    _write_toml(cfg, '[agent]\nphase_models.shipping = "opus"\n')
    monkeypatch.setattr(mt_mod, "CONFIG_PATH", cfg)
    assert resolve_phase_model("shipping") == "opus"


class TestResolveSpawnModel:
    """`resolve_spawn_model(phase, *, skills)` — most-capable-wins floor merge.

    The phase model (`resolve_phase_model`) merged with the per-skill
    `[agent.skill_models]` floors of the loaded skills. A floor only RAISES
    capability (order-independent); `None` when everything inherits.
    """

    def test_no_skill_floors_equals_phase_model(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent]\nphase_models.reviewing = "sonnet"\n')
        assert resolve_spawn_model("reviewing", skills=[], config_path=cfg) == "sonnet"

    def test_absent_config_equals_phase_model_default(self) -> None:
        # No config at all: byte-for-byte the phase-model default.
        absent = Path("/nonexistent.toml")
        for phase in ("reviewing", "testing", "shipping", "retrospecting", "planning"):
            assert resolve_spawn_model(phase, skills=["code-review"], config_path=absent) == resolve_phase_model(
                phase, config_path=absent
            )

    def test_none_when_phase_inherits_and_no_floor(self) -> None:
        absent = Path("/nonexistent.toml")
        assert resolve_spawn_model("coding", skills=["anything"], config_path=absent) is None

    def test_skill_floor_raises_above_phase_model(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.reviewing = "sonnet"\n[agent.skill_models]\ncode-review = "fable"\n',
        )
        # sonnet phase floor + a fable skill floor → fable (most capable wins).
        assert resolve_spawn_model("reviewing", skills=["code-review"], config_path=cfg) == "fable"

    def test_skill_floor_below_phase_does_not_downgrade(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.planning = "fable"\n[agent.skill_models]\ncode-review = "haiku"\n',
        )
        # A weaker skill floor never downgrades the stronger phase model.
        assert resolve_spawn_model("planning", skills=["code-review"], config_path=cfg) == "fable"

    def test_floor_merge_is_order_independent(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent.skill_models]\na = "haiku"\nb = "fable"\nc = "sonnet"\n',
        )
        # Most-capable floor wins regardless of skill order; no phase model.
        assert resolve_spawn_model("coding", skills=["a", "b", "c"], config_path=cfg) == "fable"
        assert resolve_spawn_model("coding", skills=["c", "b", "a"], config_path=cfg) == "fable"

    def test_skill_not_in_skill_models_contributes_nothing(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.skill_models]\ncode-review = "fable"\n')
        # A loaded skill with no floor entry does not raise capability.
        assert resolve_spawn_model("coding", skills=["unlisted-skill"], config_path=cfg) is None

    def test_sentinel_skill_floor_contributes_nothing(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nphase_models.reviewing = "sonnet"\n[agent.skill_models]\ncode-review = "inherit"\n',
        )
        # An inherit-sentinel floor is a no-op; the phase model stands.
        assert resolve_spawn_model("reviewing", skills=["code-review"], config_path=cfg) == "sonnet"

    def test_skill_floor_raises_an_inheriting_phase(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.skill_models]\narchitecture-design = "fable"\n')
        # coding inherits (phase model None) but a skill floor pins it up.
        assert resolve_spawn_model("coding", skills=["architecture-design"], config_path=cfg) == "fable"

    def test_inheriting_phase_floor_only_raises_when_stronger_than_assumed_opus(self, tmp_path: Path) -> None:
        # An inheriting phase (coding) has phase model None, which tier_rank
        # scores as the assumed-opus reasoning default. A per-skill floor only
        # raises it when STRICTLY stronger than that default: an `opus` (or
        # weaker) floor is silently dropped (still inherits → None), while a
        # `fable` floor raises it to the floor.
        opus_floor = tmp_path / "opus.toml"
        _write_toml(opus_floor, '[agent.skill_models]\narchitecture-design = "opus"\n')
        assert resolve_spawn_model("coding", skills=["architecture-design"], config_path=opus_floor) is None

        fable_floor = tmp_path / "fable.toml"
        _write_toml(fable_floor, '[agent.skill_models]\narchitecture-design = "fable"\n')
        assert resolve_spawn_model("coding", skills=["architecture-design"], config_path=fable_floor) == "fable"

    def test_default_config_path_used_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, '[agent.skill_models]\ncode-review = "fable"\n')
        monkeypatch.setattr(mt_mod, "CONFIG_PATH", cfg)
        import teatree.config_agent as ca_mod  # noqa: PLC0415

        monkeypatch.setattr(ca_mod, "CONFIG_PATH", cfg)
        assert resolve_spawn_model("coding", skills=["code-review"]) == "fable"


# The Fable-pinned surfaces (phase_models + skill_models + session_model) one
# config carries, used to prove the kill-switch downgrades EVERY surface, not a
# sampled subset. The ``[agent]`` scalar keys (the kill-switch + session_model)
# must precede the sub-tables, so they are injected via a template.
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
    """A Fable-pinned config: phase_models + skill_models + session_model = fable.

    *agent_scalars* are extra ``[agent]`` scalar lines (the kill-switch keys),
    placed before the phase pins so they stay inside the ``[agent]`` table.
    """
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
    (default ``opus`` = Opus 4.8) across every spawn + the session pin. Enabled
    (and absent) is byte-identical to today.
    """

    def test_disabled_downgrades_every_phase_skill_combo_to_fallback(self, tmp_path: Path) -> None:
        # Toggle OFF: NO Fable id for ANY (phase, skill-bundle) combination
        # drawn from a Fable-pinned config. The fitness invariant the kill-switch
        # guarantees is "no Fable id anywhere"; the comparison run below proves
        # that wherever ON resolved to Fable, OFF resolved to the fallback.
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
                    # Every combo that resolved to Fable when ON (short alias OR
                    # the full claude-fable-5 id) now resolves to the fallback
                    # (opus = Opus 4.8 baseline) when OFF.
                    assert off_resolved == "opus", (phase, bundle, off_resolved)
                else:
                    # A non-Fable resolution is untouched by the kill-switch.
                    assert off_resolved == on_resolved, (phase, bundle, on_resolved, off_resolved)
        assert any_was_fable, "fixture must exercise at least one Fable resolution"

    def test_enabled_is_byte_identical_to_today(self, tmp_path: Path) -> None:
        # Toggle ON explicitly: Fable pins still resolve to Fable, preserving the
        # exact id form (short alias from the phase/skill pins, full id from the
        # t3-e2e floor) — the kill-switch ON is a no-op.
        cfg = _fable_pinned_cfg(tmp_path, agent_scalars="fable_enabled = true\n")
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) == "fable"
        assert resolve_spawn_model("coding", skills=["code-review"], config_path=cfg) == "fable"
        # t3-e2e's floor is the full claude-fable-5 id, preserved byte-for-byte.
        assert resolve_spawn_model("testing", skills=["t3-e2e"], config_path=cfg) == "claude-fable-5"

    def test_absent_toggle_is_enabled_keeps_fable(self, tmp_path: Path) -> None:
        # No fable_enabled key at all == enabled, so existing pins keep Fable.
        cfg = _fable_pinned_cfg(tmp_path)
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) == "fable"
        assert resolve_spawn_model("coding", skills=["architecture-design"], config_path=cfg) == "fable"

    def test_fable_fallback_override_to_sonnet(self, tmp_path: Path) -> None:
        cfg = _fable_pinned_cfg(tmp_path, agent_scalars='fable_enabled = false\nfable_fallback = "sonnet"\n')
        assert resolve_spawn_model("planning", skills=[], config_path=cfg) == "sonnet"
        assert resolve_spawn_model("coding", skills=["code-review"], config_path=cfg) == "sonnet"

    def test_non_fable_pins_untouched_when_disabled(self, tmp_path: Path) -> None:
        # The toggle only downgrades Fable; a sonnet/haiku pin is left alone.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(
            cfg,
            '[agent]\nfable_enabled = false\nphase_models.reviewing = "sonnet"\nphase_models.retrospecting = "haiku"\n',
        )
        assert resolve_spawn_model("reviewing", skills=[], config_path=cfg) == "sonnet"
        assert resolve_spawn_model("retrospecting", skills=[], config_path=cfg) == "haiku"

    def test_inheriting_phase_stays_none_when_disabled(self, tmp_path: Path) -> None:
        # An inheriting phase (None) is not Fable — it stays None, not the fallback.
        cfg = tmp_path / ".teatree.toml"
        _write_toml(cfg, "[agent]\nfable_enabled = false\n")
        assert resolve_spawn_model("coding", skills=[], config_path=cfg) is None


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
        assert mt_mod._downgrade_fable("sonnet", cfg) == "sonnet"
        assert mt_mod._downgrade_fable("opus", cfg) == "opus"

    def test_none_unchanged_when_disabled(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=False, fable_fallback="opus")
        assert mt_mod._downgrade_fable(None, cfg) is None

    def test_fallback_override_respected(self) -> None:
        from teatree.config_agent import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig(fable_enabled=False, fable_fallback="sonnet")
        assert mt_mod._downgrade_fable("fable", cfg) == "sonnet"
