"""LoopsConfig — parses ``[loops]`` + ``[loops.<name>]`` from ``~/.teatree.toml``.

Covers: missing tables → defaults; per-loop override wins; bad cadence →
fallback; env override; cadence parser (``30s``/``5m``/``1h``).
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
        assert cfg.enabled is True
        assert cfg.default_cadence == 300
        assert cfg.parallel is True
        assert cfg.summary_dm == "errors"
        assert cfg.per_loop == {}

    def test_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(toml, "")
        cfg = LoopsConfig.load(toml)
        assert cfg.enabled is True

    def test_no_loops_table_returns_defaults(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(toml, "[teatree]\nworkspace_dir = '/tmp/x'\n")
        cfg = LoopsConfig.load(toml)
        assert cfg.enabled is True

    def test_loops_table_overrides_globals(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(
            toml,
            """
            [loops]
            enabled = false
            default_cadence = "10m"
            parallel = false
            summary_dm = "always"
            """,
        )
        cfg = LoopsConfig.load(toml)
        assert cfg.enabled is False
        assert cfg.default_cadence == 600
        assert cfg.parallel is False
        assert cfg.summary_dm == "always"

    def test_per_loop_table_recorded(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        _write_toml(
            toml,
            """
            [loops.inbox]
            cadence = "1m"

            [loops.review]
            enabled = false
            """,
        )
        cfg = LoopsConfig.load(toml)
        assert "inbox" in cfg.per_loop
        assert cfg.per_loop["inbox"].cadence_seconds == 60
        assert cfg.per_loop["review"].enabled is False


class TestLoopsConfigEnable:
    def test_default_enabled(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig()
        assert cfg.is_enabled(loop_inbox) is True

    def test_global_disabled_disables_normal_loop(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig(enabled=False)
        assert cfg.is_enabled(loop_inbox) is False

    def test_global_disabled_keeps_always_on_loop(self, loop_dispatch: MiniLoop) -> None:
        cfg = LoopsConfig(enabled=False)
        assert cfg.is_enabled(loop_dispatch) is True

    def test_per_loop_override_disables_one(self, loop_inbox: MiniLoop) -> None:
        cfg = LoopsConfig(per_loop={"inbox": LoopOverride(enabled=False)})
        assert cfg.is_enabled(loop_inbox) is False

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
