"""``t3 <overlay> wip`` — show / set the throughput dial.

``wip`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a GLOBAL-scope DB ROW — a value in
``[teatree]`` TOML is ignored on read. Integration-first: the ``set`` write is
asserted on the persisted ``ConfigSetting`` row / the resolver, exercised
through the typer ``CliRunner`` against the same ``wip`` subgroup the overlay
app builder attaches via :func:`teatree.cli.wip.register_wip_commands`.
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

from teatree.cli.wip import register_wip_commands
from teatree.config import Wip, get_effective_settings
from teatree.core.models import ConfigSetting

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register_wip_commands(app)
    return app


def _in_process_managepy_core(*args: str, overlay_name: str = "") -> None:
    """In-process stand-in for the ``config_setting`` subprocess seam (see #2622).

    The real ``set`` path delegates the ORM write to a ``python -m teatree
    config_setting set`` subprocess so it runs where ``django.setup()`` has been
    called. A subprocess is an unstoppable external the test-doctrine permits
    mocking: in-process tests replace ONLY the subprocess boundary with a
    ``call_command`` against the same management command and the test DB, so the
    write lands where the assertions can read it. The actual unbootstrapped-process
    behaviour is proven separately by :class:`TestWipSetBootstrapsDjangoInRealProcess`.
    """
    call_command(*args)


@pytest.fixture(autouse=True)
def _stub_subprocess_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the ``wip set`` subprocess delegation in-process for the CliRunner tests.

    ``_set_wip`` imports ``managepy_core`` lazily from ``teatree.cli.overlay``
    (to avoid a circular import at module load), so the patch target is the
    source module attribute.
    """
    monkeypatch.setattr("teatree.cli.overlay.managepy_core", _in_process_managepy_core)


class TestWipSet(TestCase):
    def test_set_writes_global_wip_row(self) -> None:
        result = runner.invoke(_app(), ["wip", "set", "full"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("wip") == Wip.FULL.value
        assert "full" in result.stdout

    def test_set_upserts_over_existing_row(self) -> None:
        ConfigSetting.objects.set_value("wip", Wip.MEDIUM.value)
        result = runner.invoke(_app(), ["wip", "set", "boost"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("wip") == Wip.BOOST.value

    def test_set_alias_is_normalised_to_canonical(self) -> None:
        result = runner.invoke(_app(), ["wip", "set", "high"])
        assert result.exit_code == 0
        # The canonical value is persisted, not the alias.
        assert ConfigSetting.objects.get_effective("wip") == Wip.FULL.value

    def test_set_round_trips_through_resolver(self) -> None:
        runner.invoke(_app(), ["wip", "set", "slow"])
        assert get_effective_settings().wip is Wip.SLOW

    def test_set_typo_is_rejected_and_writes_nothing(self) -> None:
        result = runner.invoke(_app(), ["wip", "set", "ludicrous"])
        assert result.exit_code == 1
        assert ConfigSetting.objects.count() == 0


class TestWipShow(TestCase):
    def test_show_reports_effective_value(self) -> None:
        ConfigSetting.objects.set_value("wip", Wip.FULL.value)
        result = runner.invoke(_app(), ["wip", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Wip.FULL.value

    def test_show_defaults_to_medium_when_unset(self) -> None:
        result = runner.invoke(_app(), ["wip", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Wip.MEDIUM.value

    def test_show_is_read_only(self) -> None:
        ConfigSetting.objects.set_value("wip", Wip.SLOW.value)
        runner.invoke(_app(), ["wip", "show"])
        # ``show`` is a pure resolver read — no row is written or cleared.
        assert ConfigSetting.objects.count() == 1
        assert ConfigSetting.objects.get_effective("wip") == Wip.SLOW.value


_UNBOOTSTRAPPED_CLI_DRIVER = """
import sys
from typer.testing import CliRunner
import typer
from teatree.cli.wip import register_wip_commands

app = typer.Typer()
register_wip_commands(app)
result = CliRunner().invoke(app, sys.argv[1:])
sys.stdout.write(result.output)
if result.exception is not None and not isinstance(result.exception, SystemExit):
    import traceback
    traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
raise SystemExit(result.exit_code)
"""
"""A subprocess driver that exercises the ``wip`` typer commands without
``django.setup()`` — reproducing the real ``t3`` console-script condition
cheaply (imports only ``teatree.cli.wip`` + Typer, not the whole CLI tree)."""


@pytest.mark.timeout(180)
class TestWipSetBootstrapsDjangoInRealProcess:
    """``wip set`` / ``show`` work from a process where Django is NOT pre-configured.

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

    The subprocess invokes ``register_wip_commands`` directly in a process with
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

    def _wip(self, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        """Invoke the ``wip`` typer subgroup in an UNbootstrapped subprocess."""
        return subprocess.run(
            [sys.executable, "-c", _UNBOOTSTRAPPED_CLI_DRIVER, "wip", *args],
            cwd=str(self._REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_set_then_show_round_trips_without_improperly_configured(self, tmp_path: Path) -> None:
        env = self._clean_env(tmp_path / "xdg")
        self._migrate(env)
        result = self._wip(env, "set", "boost")
        combined = result.stdout + result.stderr
        assert "ImproperlyConfigured" not in combined, combined
        assert "settings are not configured" not in combined, combined
        assert result.returncode == 0, combined
        # ``show`` must read the persisted dial, not silently fall back to the default.
        shown = self._wip(env, "show")
        assert shown.stdout.strip() == Wip.BOOST.value, shown.stdout + shown.stderr
