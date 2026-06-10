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
