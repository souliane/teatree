"""Tests for hook_router loop-cadence consistency (#1036).

``_tick_meta_stale`` (staleness window = cadence*2) and the
loop-registration cron-minutes computation must resolve the loop cadence
through the shared ``teatree.config.cadence_seconds`` resolver, so they
honor the DB-home ``loop_cadence_seconds`` setting and never diverge from
the real slot cadence registered by ``t3 loop``.

Integration-style: real ``hook_router`` helper, real ``teatree.config``
resolver reading the DB-home ``loop_cadence_seconds`` row; only the
clock-dependent tick-meta mtime is staged on disk.
"""

import os
import time
from pathlib import Path

import pytest
from django.test import TestCase

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _tick_meta_stale


@pytest.fixture(autouse=True)
def _teatree_engaged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the teatree opt-in marker AND the #256 auto-load opt-in active.

    These exercise the loop-registration nudge / cron-minutes mechanism, not the
    per-session opt-in gates (covered by ``test_teatree_opt_in.py``).
    """
    monkeypatch.setattr(router, "_teatree_active", lambda session_id: True)
    monkeypatch.setattr(router, "_autoload_enabled", lambda: True)


class TestCadenceResolvesFromDb(TestCase):
    """The hook cadence readers resolve their cadence from the DB (#1775, #2650).

    ``_loop_cadence_seconds`` / ``_tick_meta_stale`` resolve the GLOBAL-scope
    ``loop_cadence_seconds`` ``ConfigSetting`` row through the shared
    ``teatree.config.cadence_seconds`` resolver — never the hardcoded 720. Since
    #2650 the loop-registration directive no longer reads that global value for
    its cron: it emits one ``register_cron`` per enabled ``Loop`` row whose cron
    derives from THAT loop's own cadence (``delay_seconds`` / ``daily_at``), and
    the PreToolUse nudge reason points at the per-loop registration instead of a
    single fat-tick cron. Grouped into a TestCase class per souliane/teatree#98
    (the standalone ``@pytest.mark.django_db`` function pattern is disallowed).
    """

    @pytest.fixture(autouse=True)
    def _fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.capsys = capsys

    def test_loop_cadence_seconds_honors_db_when_env_unset(self) -> None:
        # #1036 + #1775: with no T3_LOOP_CADENCE env, the hook cadence must fall back
        # to the DB-home loop_cadence_seconds ConfigSetting row, not the hardcoded 720.
        self.monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("loop_cadence_seconds", 60)
        from hooks.scripts.hook_router import _loop_cadence_seconds  # noqa: PLC0415

        assert _loop_cadence_seconds() == 60

    def test_tick_meta_stale_uses_db_cadence_window(self) -> None:
        # #1036 + #1775: staleness window is cadence*2. With the DB-home cadence 60s
        # (env unset), a 200s-old tick-meta is stale (200 > 120). Pre-fix this read
        # env-only -> default 720 -> window 1440s -> 200 < 1440 -> NOT stale,
        # so this asserts the cadence-aware behavior (RED pre-fix, GREEN after).
        self.monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("loop_cadence_seconds", 60)

        data_home = self.tmp_path / "xdg"
        meta_dir = data_home / "teatree"
        meta_dir.mkdir(parents=True)
        meta = meta_dir / "tick-meta.json"
        meta.write_text('{"next_epoch": 0, "cadence": 60}\n', encoding="utf-8")
        old = time.time() - 200
        os.utime(meta, (old, old))
        self.monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

        assert _tick_meta_stale() is True

    def test_loop_cadence_seconds_inserts_src_on_path_when_absent(self) -> None:
        # #1036: covers the sys.path-insert + finally-cleanup branch taken
        # when the hook process does not already have teatree's src on path.
        import sys  # noqa: PLC0415

        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("loop_cadence_seconds", 120)
        src_dir = str(Path(router.__file__).resolve().parents[2] / "src")
        self.monkeypatch.setattr(sys, "path", [p for p in sys.path if p != src_dir])
        self.monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
        from hooks.scripts.hook_router import _loop_cadence_seconds  # noqa: PLC0415

        assert _loop_cadence_seconds() == 120
        assert src_dir not in sys.path


def test_loop_cadence_seconds_falls_back_to_env_when_teatree_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1036: best-effort — if teatree.config cannot resolve, the helper
    # falls back to the env-only read (covers the except branch).
    monkeypatch.setenv("T3_LOOP_CADENCE", "240")

    def _boom() -> int:
        msg = "teatree unavailable in this hook process"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.config.cadence_seconds", _boom)
    from hooks.scripts.hook_router import _loop_cadence_seconds  # noqa: PLC0415

    assert _loop_cadence_seconds() == 240
