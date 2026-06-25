"""``AgentConfig`` parsing of the ``[agent]`` table (teatree#2216).

Mirrors the ``config_speak``/``SpeakConfig`` precedent: a frozen dataclass + a
typed sub-table parser reading raw ``tomllib`` like the ``phase_models`` loader.

Three settings: ``[agent.skill_models]`` (companion-skill-name → model floor,
MODEL only — no effort axis on the per-skill floor), ``[agent] session_model``
(the interactive main-agent model pin), and ``[agent] session_effort`` (the
interactive main-agent effort pin, validated against the strict CLI scale
``low | medium | high | xhigh | max``).
"""

from pathlib import Path

import pytest

from teatree import config_agent
from teatree.config_agent import EFFORT_SCALE, AgentConfig, parse_effort, resolve_agent_config


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestEffortScale:
    def test_scale_is_the_strict_cli_scale(self) -> None:
        assert frozenset({"low", "medium", "high", "xhigh", "max"}) == EFFORT_SCALE

    def test_no_off_in_scale(self) -> None:
        assert "off" not in EFFORT_SCALE

    @pytest.mark.parametrize("value", ["low", "medium", "high", "xhigh", "max"])
    def test_parse_accepts_each_scale_value(self, value: str) -> None:
        assert parse_effort(value) == value

    def test_parse_is_case_and_whitespace_insensitive(self) -> None:
        assert parse_effort("  XHIGH ") == "xhigh"

    def test_parse_none_returns_none(self) -> None:
        assert parse_effort(None) is None

    @pytest.mark.parametrize("bogus", ["off", "ultra", "ultracode", "maximum", "none", ""])
    def test_parse_rejects_off_scale_value(self, bogus: str) -> None:
        with pytest.raises(ValueError, match="Invalid session effort"):
            parse_effort(bogus)


class TestAgentConfigDefaults:
    def test_default_is_empty_skill_models_and_no_pins(self) -> None:
        cfg = AgentConfig()
        assert cfg.skill_models == {}
        assert cfg.session_model is None
        assert cfg.session_effort is None

    def test_default_fable_kill_switch_is_enabled_with_opus_fallback(self) -> None:
        # Absent key == enabled, so existing Fable-pinned users keep Fable; the
        # fallback baseline is opus (Opus 4.8) per teatree#2237.
        cfg = AgentConfig()
        assert cfg.fable_enabled is True
        assert cfg.fable_fallback == "opus"

    def test_default_honesty_model_is_fable(self) -> None:
        # The most-honest model an honesty-critical escalation routes to is Fable
        # by default (teatree#2263); a one-line config edit retargets it.
        assert AgentConfig().honesty_model == "fable"


class TestFableKillSwitchParse:
    """``[agent] fable_enabled`` / ``fable_fallback`` parsing (teatree#2237)."""

    def test_fable_enabled_false_parsed(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, "[agent]\nfable_enabled = false\n")
        assert resolve_agent_config(config_path=cfg).fable_enabled is False

    def test_fable_enabled_true_parsed(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, "[agent]\nfable_enabled = true\n")
        assert resolve_agent_config(config_path=cfg).fable_enabled is True

    def test_absent_fable_enabled_defaults_to_enabled(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_model = "fable"\n')
        assert resolve_agent_config(config_path=cfg).fable_enabled is True

    def test_fable_fallback_parsed(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nfable_fallback = "sonnet"\n')
        assert resolve_agent_config(config_path=cfg).fable_fallback == "sonnet"

    def test_fable_fallback_normalised_through_inherit_path(self, tmp_path: Path) -> None:
        # The fallback is normalised through ``_normalize_model`` (whitespace
        # stripped); it is a model id, so it shares that boundary.
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nfable_fallback = "  opus  "\n')
        assert resolve_agent_config(config_path=cfg).fable_fallback == "opus"

    def test_absent_fable_fallback_defaults_to_opus(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, "[agent]\nfable_enabled = false\n")
        assert resolve_agent_config(config_path=cfg).fable_fallback == "opus"

    def test_missing_file_keeps_enabled_with_opus_fallback(self, tmp_path: Path) -> None:
        resolved = resolve_agent_config(config_path=tmp_path / "nope.toml")
        assert resolved.fable_enabled is True
        assert resolved.fable_fallback == "opus"

    def test_missing_agent_section_keeps_enabled_with_opus_fallback(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[teatree]\nmode = "interactive"\n')
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.fable_enabled is True
        assert resolved.fable_fallback == "opus"


class TestHonestyModelParse:
    """``[agent] honesty_model`` parsing (teatree#2263)."""

    def test_honesty_model_parsed(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nhonesty_model = "opus"\n')
        assert resolve_agent_config(config_path=cfg).honesty_model == "opus"

    def test_honesty_model_normalised_through_inherit_path(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nhonesty_model = "  fable  "\n')
        assert resolve_agent_config(config_path=cfg).honesty_model == "fable"

    def test_absent_honesty_model_defaults_to_fable(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, "[agent]\nfable_enabled = true\n")
        assert resolve_agent_config(config_path=cfg).honesty_model == "fable"

    def test_sentinel_honesty_model_falls_back_to_fable(self, tmp_path: Path) -> None:
        # An inherit-sentinel value normalises to None → falls back to a concrete
        # model id (fable), never the sentinel (the escalation must route).
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nhonesty_model = "inherit"\n')
        assert resolve_agent_config(config_path=cfg).honesty_model == "fable"

    def test_missing_file_keeps_fable(self, tmp_path: Path) -> None:
        assert resolve_agent_config(config_path=tmp_path / "nope.toml").honesty_model == "fable"


class TestSkillModelsParse:
    def test_parsed_from_agent_subtable(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent.skill_models]\n"code-review" = "opus"\narchitecture-design = "fable"\n')
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.skill_models == {"code-review": "opus", "architecture-design": "fable"}

    def test_inherit_sentinels_map_to_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(
            cfg,
            '[agent.skill_models]\na = ""\nb = "default"\nc = "inherit"\nd = "fable"\n',
        )
        resolved = resolve_agent_config(config_path=cfg)
        # Sentinels collapse to None (inherit); only a real floor survives.
        assert resolved.skill_models == {"a": None, "b": None, "c": None, "d": "fable"}

    def test_absent_skill_models_is_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_model = "fable"\n')
        assert resolve_agent_config(config_path=cfg).skill_models == {}

    def test_non_table_skill_models_falls_back_to_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nskill_models = "oops"\n')
        assert resolve_agent_config(config_path=cfg).skill_models == {}


class TestSessionModelParse:
    def test_session_model_parsed(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_model = "fable"\n')
        assert resolve_agent_config(config_path=cfg).session_model == "fable"

    @pytest.mark.parametrize("sentinel", ["", "default", "inherit"])
    def test_session_model_sentinels_map_to_none(self, tmp_path: Path, sentinel: str) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, f'[agent]\nsession_model = "{sentinel}"\n')
        assert resolve_agent_config(config_path=cfg).session_model is None

    def test_absent_session_model_is_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, "[agent]\n")
        assert resolve_agent_config(config_path=cfg).session_model is None


class TestSessionEffortParse:
    def test_session_effort_parsed(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_effort = "xhigh"\n')
        assert resolve_agent_config(config_path=cfg).session_effort == "xhigh"

    def test_session_effort_case_normalised(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_effort = "MAX"\n')
        assert resolve_agent_config(config_path=cfg).session_effort == "max"

    def test_absent_session_effort_is_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, "[agent]\n")
        assert resolve_agent_config(config_path=cfg).session_effort is None

    def test_invalid_session_effort_raises_clean_valueerror(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_effort = "off"\n')
        with pytest.raises(ValueError, match="Invalid session effort"):
            resolve_agent_config(config_path=cfg)


class TestComposesWithPhaseModels:
    def test_phase_models_and_agent_settings_coexist(self, tmp_path: Path) -> None:
        # The user's real config shape: phase_models alongside the new keys.
        cfg = tmp_path / ".teatree.toml"
        _write(
            cfg,
            "[agent]\n"
            'session_model = "fable"\n'
            'session_effort = "xhigh"\n'
            'phase_models.planning = "fable"\n'
            "[agent.skill_models]\n"
            'code-review = "opus"\n',
        )
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.session_model == "fable"
        assert resolved.session_effort == "xhigh"
        assert resolved.skill_models == {"code-review": "opus"}


class TestTierModelsParse:
    """``[agent.tier_models]`` override parsing — the model-constant escape hatch."""

    def test_parsed_from_agent_subtable(self, tmp_path: Path) -> None:
        # Sentinel override values (not real model ids) — the parser accepts any
        # string, so the test proves parsing without baking a concrete model id.
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent.tier_models]\nfrontier = "sentinel-frontier"\ncheap = "sentinel-cheap"\n')
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.tier_models == {"frontier": "sentinel-frontier", "cheap": "sentinel-cheap"}

    def test_whitespace_stripped(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent.tier_models]\nfrontier = "  sentinel-frontier  "\n')
        assert resolve_agent_config(config_path=cfg).tier_models == {"frontier": "sentinel-frontier"}

    def test_blank_and_non_string_values_skipped(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent.tier_models]\nfrontier = ""\nbalanced = "ok"\ncheap = 5\n')
        # A blank value and a non-string value are tolerated and skipped (matches
        # skill_models tolerance); only the real override survives.
        assert resolve_agent_config(config_path=cfg).tier_models == {"balanced": "ok"}

    def test_absent_tier_models_is_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_model = "fable"\n')
        assert resolve_agent_config(config_path=cfg).tier_models == {}

    def test_non_table_tier_models_falls_back_to_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\ntier_models = "oops"\n')
        assert resolve_agent_config(config_path=cfg).tier_models == {}

    def test_default_config_has_empty_tier_models(self) -> None:
        assert AgentConfig().tier_models == {}


class TestPhaseFanoutParse:
    """``[agent.phase_fanout]`` opt-in parsing (teatree#2229)."""

    def test_bool_opt_in_parsed_as_bool(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent.phase_fanout]\n"reviewer:reviewing" = true\n"author:planning" = false\n')
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.phase_fanout == {"reviewer:reviewing": True, "author:planning": False}
        # bool must stay bool, never collapse to 1/0 (bool is an int subclass).
        assert resolved.phase_fanout["reviewer:reviewing"] is True
        assert resolved.phase_fanout["author:planning"] is False

    def test_int_opt_in_overrides_width(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent.phase_fanout]\n"author:planning" = 5\n')
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.phase_fanout == {"author:planning": 5}
        assert resolved.phase_fanout["author:planning"] == 5

    def test_out_of_bounds_int_is_not_rejected_at_parse_time(self, tmp_path: Path) -> None:
        # Parse keeps the value; the out-of-range guard is fail-loud at render
        # time (core.phases._resolved_fanout_n), so the misconfiguration
        # surfaces with the rendering context, not silently dropped at parse.
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent.phase_fanout]\n"author:planning" = 9\n')
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.phase_fanout == {"author:planning": 9}

    def test_absent_phase_fanout_is_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_model = "fable"\n')
        assert resolve_agent_config(config_path=cfg).phase_fanout == {}

    def test_non_table_phase_fanout_falls_back_to_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nphase_fanout = "oops"\n')
        assert resolve_agent_config(config_path=cfg).phase_fanout == {}

    def test_non_bool_non_int_entry_values_are_skipped(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(
            cfg,
            '[agent.phase_fanout]\n"reviewer:reviewing" = true\n"author:planning" = "nonsense"\n',
        )
        # The string value is tolerated and skipped (matches skill_models
        # tolerance), the valid bool survives.
        assert resolve_agent_config(config_path=cfg).phase_fanout == {"reviewer:reviewing": True}

    def test_composes_with_phase_models_and_skill_models(self, tmp_path: Path) -> None:
        # The user's real config shape: phase_fanout alongside the existing
        # skill_models + phase_models sub-tables, all under [agent].
        cfg = tmp_path / ".teatree.toml"
        _write(
            cfg,
            "[agent]\n"
            'session_model = "fable"\n'
            'phase_models.planning = "fable"\n'
            "[agent.skill_models]\n"
            'code-review = "opus"\n'
            "[agent.phase_fanout]\n"
            '"reviewer:reviewing" = true\n'
            '"author:planning" = 4\n',
        )
        resolved = resolve_agent_config(config_path=cfg)
        assert resolved.session_model == "fable"
        assert resolved.skill_models == {"code-review": "opus"}
        assert resolved.phase_fanout == {"reviewer:reviewing": True, "author:planning": 4}

    def test_default_config_has_empty_phase_fanout(self) -> None:
        assert AgentConfig().phase_fanout == {}


class TestMalformedAndMissing:
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        assert resolve_agent_config(config_path=tmp_path / "nope.toml") == AgentConfig()

    def test_missing_agent_section_returns_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[teatree]\nmode = "interactive"\n')
        assert resolve_agent_config(config_path=cfg) == AgentConfig()

    def test_malformed_toml_returns_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, "[agent\nsession_model = not valid toml")
        assert resolve_agent_config(config_path=cfg) == AgentConfig()

    def test_non_table_agent_returns_defaults(self, tmp_path: Path) -> None:
        # ``agent`` declared as a scalar (not a table) is ignored, defaults stand.
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, 'agent = "oops"\n')
        assert resolve_agent_config(config_path=cfg) == AgentConfig()

    def test_default_config_path_used_when_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / ".teatree.toml"
        _write(cfg, '[agent]\nsession_model = "fable"\n')
        monkeypatch.setattr(config_agent, "CONFIG_PATH", cfg)
        assert resolve_agent_config().session_model == "fable"
