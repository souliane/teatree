"""LoopsConfig — parses the DB-home ``loops`` setting (its global + per-loop tables).

Covers: absent row → defaults; per-loop cadence override; bad cadence →
fallback; cadence parser (``30s``/``5m``/``1h``). Loop-disabled state is DB-only
(``LoopState``); the DB tier is pinned in ``test_loop_state_gating.py``, the config
non-read in ``test_loops_disabled_db_not_toml_2702.py``, and the env-inertness in
``test_loops_disabled_db_not_toml_2702.py`` / the review chokepoint's
``test_review_loop_db_only_control.py``.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopOverride, LoopsConfig, parse_cadence


def _build_jobs(**_: object) -> list[object]:
    return []


@pytest.fixture
def loop_inbox() -> MiniLoop:
    return MiniLoop(name="inbox", default_cadence_seconds=60, build_jobs=_build_jobs)


@pytest.fixture
def loop_dispatch() -> MiniLoop:
    return MiniLoop(
        name="dispatch",
        default_cadence_seconds=300,
        build_jobs=_build_jobs,
    )


def _seed_loops(db: Path, table: dict[str, object]) -> None:
    """Seed the DB-home ``loops`` setting the cold reader resolves."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            ("", "loops", json.dumps(table)),
        )
        conn.commit()
    finally:
        conn.close()


class TestParseCadence:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("30s", 60),  # floor 60
            ("60s", 60),
            ("90s", 90),
            ("5m", 300),
            ("1h", 3600),
            ("2h", 7200),
            (300, 300),
            (45, 60),  # int floor too
        ],
    )
    def test_well_formed(self, raw: object, expected: int) -> None:
        assert parse_cadence(raw, default=300) == expected

    def test_bad_value_falls_back(self) -> None:
        assert parse_cadence("not-a-cadence", default=300) == 300

    def test_none_falls_back(self) -> None:
        assert parse_cadence(None, default=300) == 300


class TestLoopsConfigLoad:
    def test_absent_db_returns_defaults(self, tmp_path: Path) -> None:
        cfg = LoopsConfig.load(tmp_path / "missing.sqlite3")
        assert cfg.default_cadence == 300
        assert cfg.parallel is True
        assert cfg.summary_dm == "errors"
        assert cfg.per_loop == {}

    def test_empty_loops_table_returns_defaults(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _seed_loops(db, {})
        cfg = LoopsConfig.load(db)
        assert cfg.default_cadence == 300

    def test_absent_loops_row_returns_defaults(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        cfg = LoopsConfig.load(db)
        assert cfg.default_cadence == 300

    def test_loops_table_overrides_globals(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _seed_loops(db, {"default_cadence": "10m", "parallel": False, "summary_dm": "always"})
        cfg = LoopsConfig.load(db)
        assert cfg.default_cadence == 600
        assert cfg.parallel is False
        assert cfg.summary_dm == "always"

    def test_per_loop_cadence_recorded(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _seed_loops(db, {"inbox": {"cadence": "1m"}})
        cfg = LoopsConfig.load(db)
        assert "inbox" in cfg.per_loop
        assert cfg.per_loop["inbox"].cadence_seconds == 60

    def test_loops_enabled_config_key_is_not_read(self, tmp_path: Path) -> None:
        # #2702: the global ``enabled`` / per-loop ``enabled`` keys are no longer
        # read — only cadence/parallel/summary come from the ``loops`` setting now.
        db = tmp_path / "db.sqlite3"
        _seed_loops(db, {"enabled": False, "review": {"enabled": False, "cadence": "1m"}})
        cfg = LoopsConfig.load(db)
        assert cfg.per_loop["review"].cadence_seconds == 60
        assert not hasattr(cfg, "enabled")
        assert not hasattr(cfg.per_loop["review"], "enabled")


class TestLoopsConfigEnable:
    def test_default_enabled(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig()
        assert cfg.is_enabled(loop_inbox) is True

    def test_env_kill_switch_is_inert(
        self,
        loop_inbox: MiniLoop,
        loop_dispatch: MiniLoop,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``T3_LOOPS_DISABLED`` is removed — loop control is DB-only. A set env
        # value (named or the ``all`` sentinel) has NO effect; with no DB hold
        # every loop stays enabled. (DB-disable is the control outcome — pinned
        # in test_loop_state_gating.py / test_loops_disabled_db_not_toml_2702.py.)
        monkeypatch.setenv("T3_LOOPS_DISABLED", "all")
        cfg = LoopsConfig()
        assert cfg.is_enabled(loop_inbox) is True
        assert cfg.is_enabled(loop_dispatch) is True


class TestLoopsConfigCadence:
    def test_cadence_for_returns_default(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig()
        assert cfg.cadence_for(loop_inbox) == 60

    def test_cadence_for_returns_per_loop_override(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig(per_loop={"inbox": LoopOverride(cadence_seconds=120)})
        assert cfg.cadence_for(loop_inbox) == 120

    def test_cadence_for_falls_back_when_override_is_none(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig(per_loop={"inbox": LoopOverride()})
        assert cfg.cadence_for(loop_inbox) == 60


class TestLoopsConfigLegacyShim:
    def test_bad_default_cadence_falls_back_to_300(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _seed_loops(db, {"default_cadence": "garbage"})
        cfg = LoopsConfig.load(db)
        assert cfg.default_cadence == 300

    def test_bad_per_loop_cadence_falls_back_to_none(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _seed_loops(db, {"inbox": {"cadence": "junk"}})
        cfg = LoopsConfig.load(db)
        # Bad cadence value silently degrades to "no override" so the
        # loop's own default cadence wins.
        assert cfg.per_loop["inbox"].cadence_seconds is None
