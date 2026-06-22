"""``t3 <overlay> speed`` — show / set the throughput dial.

``speed`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a GLOBAL-scope DB ROW — a value in
``[teatree]`` TOML is ignored on read. Integration-first: the ``set`` write is
asserted on the persisted ``ConfigSetting`` row / the resolver, exercised
through the typer ``CliRunner`` against the same ``speed`` subgroup the overlay
app builder attaches via :func:`teatree.cli.speed.register_speed_commands`.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from django.core.management import call_command
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.speed import register_speed_commands
from teatree.config import Speed, get_effective_settings
from teatree.core.models import ConfigSetting

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register_speed_commands(app)
    return app


def _in_process_managepy_core(*args: str, overlay_name: str = "") -> None:
    """In-process stand-in for the ``config_setting`` subprocess seam (see #2622).

    The real ``set`` path delegates the ORM write to a ``python -m teatree
    config_setting set`` subprocess so it runs where ``django.setup()`` has been
    called. A subprocess is an unstoppable external the test-doctrine permits
    mocking: in-process tests replace ONLY the subprocess boundary with a
    ``call_command`` against the same management command and the test DB, so the
    write lands where the assertions can read it. The actual unbootstrapped-process
    behaviour is proven separately by :class:`TestSpeedSetBootstrapsDjangoInRealProcess`.
    """
    call_command(*args)


@pytest.fixture(autouse=True)
def _stub_subprocess_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the ``speed set`` subprocess delegation in-process for the CliRunner tests.

    ``_set_speed`` imports ``managepy_core`` lazily from ``teatree.cli.overlay``
    (to avoid a circular import at module load), so the patch target is the
    source module attribute.
    """
    monkeypatch.setattr("teatree.cli.overlay.managepy_core", _in_process_managepy_core)


class TestSpeedSet(TestCase):
    def test_set_writes_global_speed_row(self) -> None:
        result = runner.invoke(_app(), ["speed", "set", "full"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("speed") == Speed.FULL.value
        assert "full" in result.stdout

    def test_set_upserts_over_existing_row(self) -> None:
        ConfigSetting.objects.set_value("speed", Speed.MEDIUM.value)
        result = runner.invoke(_app(), ["speed", "set", "boost"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("speed") == Speed.BOOST.value

    def test_set_alias_is_normalised_to_canonical(self) -> None:
        result = runner.invoke(_app(), ["speed", "set", "high"])
        assert result.exit_code == 0
        # The canonical value is persisted, not the alias.
        assert ConfigSetting.objects.get_effective("speed") == Speed.FULL.value

    def test_set_round_trips_through_resolver(self) -> None:
        runner.invoke(_app(), ["speed", "set", "slow"])
        assert get_effective_settings().speed is Speed.SLOW

    def test_set_typo_is_rejected_and_writes_nothing(self) -> None:
        result = runner.invoke(_app(), ["speed", "set", "ludicrous"])
        assert result.exit_code == 1
        assert ConfigSetting.objects.count() == 0


class TestSpeedShow(TestCase):
    def test_show_reports_effective_value(self) -> None:
        ConfigSetting.objects.set_value("speed", Speed.FULL.value)
        result = runner.invoke(_app(), ["speed", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Speed.FULL.value

    def test_show_defaults_to_medium_when_unset(self) -> None:
        result = runner.invoke(_app(), ["speed", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Speed.MEDIUM.value

    def test_show_is_read_only(self) -> None:
        ConfigSetting.objects.set_value("speed", Speed.SLOW.value)
        runner.invoke(_app(), ["speed", "show"])
        # ``show`` is a pure resolver read — no row is written or cleared.
        assert ConfigSetting.objects.count() == 1
        assert ConfigSetting.objects.get_effective("speed") == Speed.SLOW.value


_UNBOOTSTRAPPED_CLI_DRIVER = """
import sys
from typer.testing import CliRunner
import typer
from teatree.cli.speed import register_speed_commands

app = typer.Typer()
register_speed_commands(app)
result = CliRunner().invoke(app, sys.argv[1:])
sys.stdout.write(result.output)
if result.exception is not None and not isinstance(result.exception, SystemExit):
    import traceback
    traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
raise SystemExit(result.exit_code)
"""
"""A subprocess driver that exercises the ``speed`` typer commands without
``django.setup()`` — reproducing the real ``t3`` console-script condition
cheaply (imports only ``teatree.cli.speed`` + Typer, not the whole CLI tree)."""


@pytest.mark.timeout(180)
class TestSpeedSetBootstrapsDjangoInRealProcess:
    """``speed set`` / ``show`` work from a process where Django is NOT pre-configured.

    No in-process DB: each case spawns a clean subprocess against its OWN
    isolated ``XDG_DATA_HOME`` SQLite control DB and asserts only on subprocess
    output — so the class needs neither ``TestCase`` nor ``@pytest.mark.django_db``.

    The in-process :class:`~typer.testing.CliRunner` tests above all run inside
    pytest, where ``django.setup()`` has already configured settings, so they
    cannot observe souliane/teatree#2622: the real ``t3`` console-script process
    never runs ``django.setup()`` before dispatching the typer overlay app, so
    ``set`` crashed with ``ImproperlyConfigured`` the moment it touched the
    ``ConfigSetting`` ORM, and ``show`` silently reported the dataclass default
    (its DB tier fails safe to ``{}`` when Django is unconfigured).

    The subprocess invokes ``register_speed_commands`` directly in a process with
    no ``DJANGO_SETTINGS_MODULE`` — RED on the unbootstrapped code, GREEN once
    ``set`` delegates to the subprocess seam and ``show`` bootstraps Django.
    """

    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_ROOT = _REPO_ROOT / "src"

    def _clean_env(self, data_home: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        env["XDG_DATA_HOME"] = str(data_home)
        env["PYTHONPATH"] = os.pathsep.join([str(self._SRC_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        return env

    def _migrate(self, env: dict[str, str]) -> None:
        subprocess.run(
            [sys.executable, "-m", "teatree", "migrate", "--no-input"],
            cwd=str(self._REPO_ROOT),
            env={**env, "DJANGO_SETTINGS_MODULE": "teatree.settings"},
            capture_output=True,
            text=True,
            check=True,
        )

    def _speed(self, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        """Invoke the ``speed`` typer subgroup in an UNbootstrapped subprocess."""
        return subprocess.run(
            [sys.executable, "-c", _UNBOOTSTRAPPED_CLI_DRIVER, "speed", *args],
            cwd=str(self._REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_set_then_show_round_trips_without_improperly_configured(self, tmp_path: Path) -> None:
        env = self._clean_env(tmp_path / "xdg")
        self._migrate(env)
        result = self._speed(env, "set", "boost")
        combined = result.stdout + result.stderr
        assert "ImproperlyConfigured" not in combined, combined
        assert "settings are not configured" not in combined, combined
        assert result.returncode == 0, combined
        # ``show`` must read the persisted dial, not silently fall back to the default.
        shown = self._speed(env, "show")
        assert shown.stdout.strip() == Speed.BOOST.value, shown.stdout + shown.stderr
