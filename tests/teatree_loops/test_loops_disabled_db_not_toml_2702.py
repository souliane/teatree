"""Loop-disabled state resolves via DB ``LoopState`` only, never env or ``[loops]`` toml.

Loop control is DB-only: ``loops/config.py:is_enabled`` resolves purely through
the DB ``LoopState`` tier — there is no ``T3_LOOPS_DISABLED`` env kill-switch and
no ``[loops]``/``[loops.<name>]`` ``enabled`` toml fallback.

These pin three things, anti-vacuously. First, a ``[loops] enabled = false`` toml
file does NOT disable a loop (the toml ``enabled`` key is ignored; DB/default
authoritative) — same for the per-loop key. Second, a DB ``LoopState``
pause/disable IS honoured with no toml present (the DB tier is the authority —
must not regress). Third, a set ``T3_LOOPS_DISABLED`` env var is INERT (RED on
the pre-cutover env-tier code, which disabled the loop; GREEN now) while a DB
``LoopState`` DISABLE still suppresses regardless of the (inert) env — the same
control outcome the env tier used to provide, now DB-only.
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


def _loop(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=_build)


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

    def test_db_disable_skips_the_dispatch_loop_with_no_toml_no_env(self) -> None:
        # Even the core ``dispatch`` loop is stopped by a DB DISABLE (the env
        # tier that once exempted an ``always_on`` loop is gone).
        LoopState.objects.disable("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch")) is False


class TestEnvKillSwitchIsInert(TestCase):
    """``T3_LOOPS_DISABLED`` is removed: a set env var is INERT — the DB is the only control."""

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

    def test_env_named_loop_does_not_disable_with_db_enabled(self) -> None:
        LoopState.objects.enable("review")  # DB says runnable
        self._with_env("review")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is True  # env inert

    def test_env_all_sentinel_does_not_disable_with_db_enabled(self) -> None:
        LoopState.objects.enable("review")
        self._with_env("all")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is True  # env inert

    def test_db_disable_still_suppresses_regardless_of_inert_env(self) -> None:
        # The DB is the only control: a DISABLED row suppresses even with an env
        # value set (which no longer does anything).
        LoopState.objects.disable("review")
        self._with_env("")  # env says "not disabled" — still suppressed by DB
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False
