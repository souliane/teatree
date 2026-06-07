"""Tests for per-phase headless model tiering (#880, #562 §3).

Mechanical phases resolve to a cheaper model tier; judgment phases keep
the user's default model. The mapping is config-driven via
``~/.teatree.toml [agent] phase_models.<phase>``.
"""

from pathlib import Path

import pytest

import teatree.agents.model_tiering as mt_mod
from teatree.agents.model_tiering import DEFAULT_PHASE_MODELS, resolve_phase_model


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
