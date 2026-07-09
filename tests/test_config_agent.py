"""``AgentConfig`` resolution of the ``[agent]`` settings (teatree#2216).

Each ``[agent]`` value is its own DB key in the ``ConfigSetting`` store
(``agent_session_model``, ``agent_skill_models``, …), read Django-free via
``teatree.config.cold_reader``. Tests seed a temp sqlite with the
``teatree_config_setting`` row and point ``T3_CONFIG_DB`` at it; a test that
seeds nothing exercises the fail-to-defaults path (the autouse ``_isolate_env``
fixture leaves the cold reader with no DB).
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.config_agent import EFFORT_SCALE, AgentConfig, parse_effort, resolve_agent_config


def _seed(db_path: Path, key: str, value: object, scope: str = "") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
        (scope, key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def _point_at(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("T3_CONFIG_DB", str(db_path))


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

    def test_default_honesty_model_is_opus(self) -> None:
        # The most-honest model an honesty-critical escalation routes to defaults
        # to opus (teatree#2263, #2237 removal) — requiring no operator opt-in;
        # a stronger/different escalation target is a one-line config edit.
        assert AgentConfig().honesty_model == "opus"

    def test_absent_db_resolves_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No T3_CONFIG_DB (the autouse fixture cleared it) → no cold-read → the
        # dataclass defaults stand, unchanged until a value is stored.
        monkeypatch.delenv("T3_CONFIG_DB", raising=False)
        assert resolve_agent_config() == AgentConfig()


class TestHonestyModelParse:
    """``agent_honesty_model`` parsing (teatree#2263)."""

    def test_honesty_model_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_honesty_model", "sonnet")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().honesty_model == "sonnet"

    def test_honesty_model_normalised_through_inherit_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_honesty_model", "  opus  ")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().honesty_model == "opus"

    def test_absent_honesty_model_defaults_to_opus(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "opus")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().honesty_model == "opus"

    def test_sentinel_honesty_model_falls_back_to_opus(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An inherit-sentinel value normalises to None → falls back to a concrete
        # model id (opus), never the sentinel (the escalation must route).
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_honesty_model", "inherit")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().honesty_model == "opus"

    def test_missing_file_keeps_opus(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _point_at(monkeypatch, tmp_path / "nope.sqlite3")
        assert resolve_agent_config().honesty_model == "opus"


class TestSkillModelsParse:
    def test_parsed_from_db_dict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_skill_models", {"code-review": "opus", "architecture-design": "haiku"})
        _point_at(monkeypatch, db)
        assert resolve_agent_config().skill_models == {"code-review": "opus", "architecture-design": "haiku"}

    def test_inherit_sentinels_map_to_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_skill_models", {"a": "", "b": "default", "c": "inherit", "d": "haiku"})
        _point_at(monkeypatch, db)
        # Sentinels collapse to None (inherit); only a real floor survives.
        assert resolve_agent_config().skill_models == {"a": None, "b": None, "c": None, "d": "haiku"}

    def test_absent_skill_models_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().skill_models == {}

    def test_non_dict_skill_models_falls_back_to_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_skill_models", "oops")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().skill_models == {}


class TestSessionModelParse:
    def test_session_model_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().session_model == "haiku"

    @pytest.mark.parametrize("sentinel", ["", "default", "inherit"])
    def test_session_model_sentinels_map_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel: str
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", sentinel)
        _point_at(monkeypatch, db)
        assert resolve_agent_config().session_model is None

    def test_absent_session_model_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_effort", "xhigh")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().session_model is None


class TestSessionEffortParse:
    def test_session_effort_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_effort", "xhigh")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().session_effort == "xhigh"

    def test_session_effort_case_normalised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_effort", "MAX")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().session_effort == "max"

    def test_absent_session_effort_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().session_effort is None

    def test_invalid_session_effort_raises_clean_valueerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_effort", "off")
        _point_at(monkeypatch, db)
        with pytest.raises(ValueError, match="Invalid session effort"):
            resolve_agent_config()


class TestComposesWithPhaseModels:
    def test_agent_settings_coexist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The user's real config shape: several agent keys stored side by side.
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _seed(db, "agent_session_effort", "xhigh")
        _seed(db, "agent_skill_models", {"code-review": "opus"})
        _point_at(monkeypatch, db)
        resolved = resolve_agent_config()
        assert resolved.session_model == "haiku"
        assert resolved.session_effort == "xhigh"
        assert resolved.skill_models == {"code-review": "opus"}


class TestTierModelsParse:
    """``agent_tier_models`` override parsing — the model-constant escape hatch."""

    def test_parsed_from_db_dict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Sentinel override values (not real model ids) — the parser accepts any
        # string, so the test proves parsing without baking a concrete model id.
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_models", {"frontier": "sentinel-frontier", "cheap": "sentinel-cheap"})
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_models == {"frontier": "sentinel-frontier", "cheap": "sentinel-cheap"}

    def test_whitespace_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_models", {"frontier": "  sentinel-frontier  "})
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_models == {"frontier": "sentinel-frontier"}

    def test_blank_and_non_string_values_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_models", {"frontier": "", "balanced": "ok", "cheap": 5})
        _point_at(monkeypatch, db)
        # A blank value and a non-string value are tolerated and skipped (matches
        # skill_models tolerance); only the real override survives.
        assert resolve_agent_config().tier_models == {"balanced": "ok"}

    def test_absent_tier_models_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_models == {}

    def test_non_dict_tier_models_falls_back_to_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_models", "oops")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_models == {}

    def test_default_config_has_empty_tier_models(self) -> None:
        assert AgentConfig().tier_models == {}


class TestTierEffortParse:
    """``agent_tier_effort`` override parsing — the per-tier reasoning-effort dial."""

    def test_parsed_from_db_dict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_effort", {"frontier": "max", "balanced": "xhigh"})
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_effort == {"frontier": "max", "balanced": "xhigh"}

    def test_case_and_whitespace_normalised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mirrors ``parse_effort`` normalisation (lower + strip) so the stored
        # override is always a canonical EFFORT_SCALE member.
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_effort", {"frontier": "  XHIGH  "})
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_effort == {"frontier": "xhigh"}

    def test_off_scale_and_non_string_values_dropped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_effort", {"frontier": "off", "balanced": "high", "cheap": 5})
        _point_at(monkeypatch, db)
        # An off-scale value ("off") and a non-string value are dropped (matches
        # tier_models tolerance); only the valid scale value survives.
        assert resolve_agent_config().tier_effort == {"balanced": "high"}

    def test_absent_tier_effort_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_effort == {}

    def test_non_dict_tier_effort_falls_back_to_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_tier_effort", "oops")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().tier_effort == {}

    def test_default_config_has_empty_tier_effort(self) -> None:
        assert AgentConfig().tier_effort == {}


class TestPhaseFanoutParse:
    """``agent_phase_fanout`` opt-in parsing (teatree#2229)."""

    def test_bool_opt_in_parsed_as_bool(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_phase_fanout", {"reviewer:reviewing": True, "author:planning": False})
        _point_at(monkeypatch, db)
        resolved = resolve_agent_config()
        assert resolved.phase_fanout == {"reviewer:reviewing": True, "author:planning": False}
        # bool must stay bool, never collapse to 1/0 (bool is an int subclass).
        assert resolved.phase_fanout["reviewer:reviewing"] is True
        assert resolved.phase_fanout["author:planning"] is False

    def test_int_opt_in_overrides_width(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_phase_fanout", {"author:planning": 5})
        _point_at(monkeypatch, db)
        resolved = resolve_agent_config()
        assert resolved.phase_fanout == {"author:planning": 5}
        assert resolved.phase_fanout["author:planning"] == 5

    def test_out_of_bounds_int_is_not_rejected_at_parse_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Parse keeps the value; the out-of-range guard is fail-loud at render
        # time (core.phases._resolved_fanout_n), so the misconfiguration
        # surfaces with the rendering context, not silently dropped at parse.
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_phase_fanout", {"author:planning": 9})
        _point_at(monkeypatch, db)
        assert resolve_agent_config().phase_fanout == {"author:planning": 9}

    def test_absent_phase_fanout_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().phase_fanout == {}

    def test_non_dict_phase_fanout_falls_back_to_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_phase_fanout", "oops")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().phase_fanout == {}

    def test_non_bool_non_int_entry_values_are_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_phase_fanout", {"reviewer:reviewing": True, "author:planning": "nonsense"})
        _point_at(monkeypatch, db)
        # The string value is tolerated and skipped (matches skill_models
        # tolerance), the valid bool survives.
        assert resolve_agent_config().phase_fanout == {"reviewer:reviewing": True}

    def test_composes_with_skill_models(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The user's real config shape: phase_fanout alongside the existing
        # skill_models keys.
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _seed(db, "agent_skill_models", {"code-review": "opus"})
        _seed(db, "agent_phase_fanout", {"reviewer:reviewing": True, "author:planning": 4})
        _point_at(monkeypatch, db)
        resolved = resolve_agent_config()
        assert resolved.session_model == "haiku"
        assert resolved.skill_models == {"code-review": "opus"}
        assert resolved.phase_fanout == {"reviewer:reviewing": True, "author:planning": 4}

    def test_default_config_has_empty_phase_fanout(self) -> None:
        assert AgentConfig().phase_fanout == {}


class TestMalformedAndMissing:
    def test_missing_db_returns_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _point_at(monkeypatch, tmp_path / "nope.sqlite3")
        assert resolve_agent_config() == AgentConfig()

    def test_absent_keys_return_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unrelated key present, none of the agent keys → defaults stand.
        db = tmp_path / "db.sqlite3"
        _seed(db, "mode", "interactive")
        _point_at(monkeypatch, db)
        assert resolve_agent_config() == AgentConfig()

    def test_env_pointed_db_drives_the_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # T3_CONFIG_DB is the resolution seam: the cold reader reads exactly the
        # DB it points at (no config file anywhere).
        db = tmp_path / "db.sqlite3"
        _seed(db, "agent_session_model", "haiku")
        _point_at(monkeypatch, db)
        assert resolve_agent_config().session_model == "haiku"
