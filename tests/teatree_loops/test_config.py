"""LoopsConfig — parses ``[loops]`` + ``[loops.<name>]`` from ``~/.teatree.toml``.

Covers: missing tables → defaults; per-loop cadence override; bad cadence →
fallback; env override; cadence parser (``30s``/``5m``/``1h``). Loop-disabled
state is env → DB ``LoopState`` → default (#2702 — no ``[loops] enabled`` toml
fallback); the DB tier is pinned in ``test_loop_state_gating.py`` and the toml
non-read in ``test_loops_disabled_db_not_toml_2702.py``.
"""

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
        always_on=True,
    )


def _write_toml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


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
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        cfg = LoopsConfig.load(tmp_path / "missing.toml")
        assert cfg.default_cadence == 300
        assert cfg.parallel is True
        assert cfg.summary_dm == "errors"
        assert cfg.per_loop == {}

    def test_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(toml, "")
        cfg = LoopsConfig.load(toml)
        assert cfg.default_cadence == 300

    def test_no_loops_table_returns_defaults(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(toml, "[teatree]\nworkspace_dir = '/tmp/x'\n")
        cfg = LoopsConfig.load(toml)
        assert cfg.default_cadence == 300

    def test_loops_table_overrides_globals(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(
            toml,
            """
            [loops]
            default_cadence = "10m"
            parallel = false
            summary_dm = "always"
            """,
        )
        cfg = LoopsConfig.load(toml)
        assert cfg.default_cadence == 600
        assert cfg.parallel is False
        assert cfg.summary_dm == "always"

    def test_per_loop_cadence_recorded(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(
            toml,
            """
            [loops.inbox]
            cadence = "1m"
            """,
        )
        cfg = LoopsConfig.load(toml)
        assert "inbox" in cfg.per_loop
        assert cfg.per_loop["inbox"].cadence_seconds == 60

    def test_loops_enabled_toml_key_is_not_read(self, tmp_path: Path) -> None:
        # #2702: the [loops] enabled / [loops.<name>] enabled toml keys are no
        # longer read — only cadence/parallel/summary come from toml now.
        toml = tmp_path / "t.toml"
        _write_toml(
            toml,
            """
            [loops]
            enabled = false

            [loops.review]
            enabled = false
            cadence = "1m"
            """,
        )
        cfg = LoopsConfig.load(toml)
        assert cfg.per_loop["review"].cadence_seconds == 60
        assert not hasattr(cfg, "enabled")
        assert not hasattr(cfg.per_loop["review"], "enabled")


class TestLoopsConfigEnable:
    def test_default_enabled(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig()
        assert cfg.is_enabled(loop_inbox) is True

    def test_env_disables_named_loops(
        self,
        loop_inbox: MiniLoop,
        loop_dispatch: MiniLoop,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_LOOPS_DISABLED", "inbox,review")
        cfg = LoopsConfig()
        assert cfg.is_enabled(loop_inbox) is False
        # always_on respects the env list too — env is the user's hard kill switch.
        assert cfg.is_enabled(loop_dispatch) is True

    def test_env_kills_global_when_all(self, loop_inbox: MiniLoop, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_LOOPS_DISABLED", "all")
        cfg = LoopsConfig()
        assert cfg.is_enabled(loop_inbox) is False


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
        toml = tmp_path / "t.toml"
        _write_toml(toml, '[loops]\ndefault_cadence = "garbage"\n')
        cfg = LoopsConfig.load(toml)
        assert cfg.default_cadence == 300

    def test_bad_per_loop_cadence_falls_back_to_none(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(
            toml,
            """
            [loops.inbox]
            cadence = "junk"
            """,
        )
        cfg = LoopsConfig.load(toml)
        # Bad cadence value silently degrades to "no override" so the
        # loop's own default cadence wins.
        assert cfg.per_loop["inbox"].cadence_seconds is None
