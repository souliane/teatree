"""Loop-disabled state resolves env → DB ``LoopState`` → default, never ``[loops]`` toml (#2702).

The last #2697 config-toml→DB bypass reader (B6): ``T3_LOOPS_DISABLED`` read env
FIRST (a deliberate platform-leaf hard kill-switch, settled by #2359 — kept
byte-for-byte) but then fell back to a ``tomllib.load()`` of the ``[loops]`` toml
section for the *disabled* decision. That toml fallback is the bypass: the DB
``LoopState`` tier (``loops/config.py:is_enabled``) is already the authority, so
loop-disabled state must resolve env → DB ``LoopState`` → default with the
``[loops]``/``[loops.<name>]`` ``enabled`` toml keys no longer read.

These pin three things that together are anti-vacuous (RED on pre-#2702 code).
First, a ``[loops] enabled = false`` toml file with NO env set NO LONGER disables
a loop — RED before (the toml fallback read it and disabled), GREEN after (the
toml ``enabled`` key is ignored; DB/default authoritative). Same for the per-loop
``[loops.<name>] enabled = false`` key. Second, a DB ``LoopState`` pause/disable
IS honoured with no toml present and no env set (the DB tier stays authoritative
— must not regress). Third, the env kill-switch STILL overrides regardless of the
DB (env set → disabled), proving the #2359 env-first path is untouched.
"""

import os
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.models import LoopState
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig


def _build(**_: object) -> list[object]:
    return []


def _loop(name: str, *, always_on: bool = False) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=_build, always_on=always_on)


class _TomlTestCase(TestCase):
    def _toml(self, body: str) -> Path:
        path = Path(tempfile.mkdtemp()) / "t.toml"
        path.write_text(body, encoding="utf-8")
        return path


class TestTomlEnabledKeyNoLongerDisables(_TomlTestCase):
    """The ``[loops]`` ``enabled`` toml keys are no longer read for the disabled decision."""

    def test_global_loops_enabled_false_toml_does_not_disable(self) -> None:
        config = LoopsConfig.load(self._toml("[loops]\nenabled = false\n"))
        # No env, no DB row → loop runs; the toml `enabled = false` is ignored.
        assert config.is_enabled(_loop("review")) is True

    def test_per_loop_enabled_false_toml_does_not_disable(self) -> None:
        config = LoopsConfig.load(self._toml("[loops.review]\nenabled = false\n"))
        assert config.is_enabled(_loop("review")) is True


class TestDbTierHonouredWithoutTomlOrEnv(TestCase):
    """Loop-disabled state resolves via the DB tier with NO ``[loops]`` toml and NO env."""

    def test_db_pause_disables_with_no_toml_no_env(self) -> None:
        LoopState.objects.pause("review")
        config = LoopsConfig()  # no toml file read, defaults only
        assert config.is_enabled(_loop("review")) is False

    def test_db_disable_disables_with_no_toml_no_env(self) -> None:
        LoopState.objects.disable("review")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False

    def test_no_db_row_no_toml_no_env_defaults_to_enabled(self) -> None:
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is True

    def test_db_disable_skips_an_always_on_loop_with_no_toml_no_env(self) -> None:
        LoopState.objects.disable("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch", always_on=True)) is False


class TestEnvKillSwitchStillOverridesDb(TestCase):
    """The #2359 env-first kill-switch is untouched: env set → disabled regardless of the DB."""

    def _with_env(self, value: str) -> None:
        old = os.environ.get("T3_LOOPS_DISABLED")
        os.environ["T3_LOOPS_DISABLED"] = value
        self.addCleanup(self._restore_env, old)

    @staticmethod
    def _restore_env(old: str | None) -> None:
        if old is None:
            os.environ.pop("T3_LOOPS_DISABLED", None)
        else:
            os.environ["T3_LOOPS_DISABLED"] = old

    def test_env_disables_named_loop_even_with_db_enabled(self) -> None:
        LoopState.objects.enable("review")  # DB says runnable
        self._with_env("review")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False

    def test_env_all_sentinel_disables_even_with_db_enabled(self) -> None:
        LoopState.objects.enable("review")
        self._with_env("all")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False
