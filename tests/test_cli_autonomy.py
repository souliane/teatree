"""``t3 <overlay> autonomy`` — show / set the per-overlay trust switch.

The first-class CLI surface for the single ``autonomy`` knob (souliane/teatree
#1668) that collapses the three user-approval gates — colleague auto-approve
(``on_behalf_post_mode``), auto-merge (``require_human_approval_to_merge``),
and answer (``require_human_approval_to_answer``) — so a user flips an overlay
to full merge/approve autonomy with one command instead of hand-editing config.

``autonomy`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a DB ROW (the active/``--overlay``
overlay's OVERLAY-scoped row by default, the GLOBAL-scope row with ``--global``)
— a ``[teatree]`` / ``[overlays.<name>]`` TOML value is ignored on read.
Integration-first: the ``set`` write is asserted on the persisted
``ConfigSetting`` row and round-tripped through :func:`get_effective_settings`
so the gates collapse — and the safety floor does NOT.
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

from teatree.cli.autonomy import register_autonomy_commands
from teatree.config import Autonomy, Mode, OnBehalfPostMode, get_effective_settings
from teatree.core.models import ConfigSetting

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register_autonomy_commands(app)
    return app


def _in_process_managepy_core(*args: str, overlay_name: str = "") -> None:
    """In-process stand-in for the ``config_setting`` subprocess seam.

    The real ``set`` path delegates the ORM write to a ``python -m teatree
    config_setting set`` subprocess (#2622) so it runs where ``django.setup()``
    has been called. A subprocess is an unstoppable external the test-doctrine
    permits mocking: in-process tests replace ONLY the subprocess boundary with
    a ``call_command`` against the same management command and the test DB, so
    the typer command's resolution logic and the real delegation arg shape are
    still exercised, and the row lands where the assertions can read it. The
    actual unbootstrapped-process behaviour is proven separately by
    :class:`TestAutonomySetBootstrapsDjangoInRealProcess`.
    """
    call_command(*args)


@pytest.fixture(autouse=True)
def _stub_subprocess_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the ``autonomy set`` subprocess delegation in-process for the CliRunner tests.

    ``_write_setting_row`` imports ``managepy_core`` lazily from
    ``teatree.cli.overlay`` (to avoid a circular import at module load), so the
    patch target is the source module attribute, not a re-export on
    ``teatree.cli.autonomy``.
    """
    monkeypatch.setattr("teatree.cli.overlay.managepy_core", _in_process_managepy_core)


class TestAutonomySetGlobal(TestCase):
    def test_global_writes_global_scope_row(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "set", "full", "--global"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.FULL.value
        assert "global config store" in result.stdout

    def test_global_upserts_over_existing_row(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.BABYSIT.value)
        result = runner.invoke(_app(), ["autonomy", "set", "notify", "--global"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.NOTIFY.value

    def test_global_leaves_overlay_scope_untouched(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.NOTIFY.value, scope="t3-teatree")
        runner.invoke(_app(), ["autonomy", "set", "babysit", "--global"])
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.BABYSIT.value
        # The overlay-scoped row is a distinct ``(scope, key)`` and is preserved.
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-teatree") == Autonomy.NOTIFY.value


class TestAutonomySetPerOverlay(TestCase):
    @pytest.fixture(autouse=True)
    def _fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_named_overlay_writes_overlay_scope_row(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "t3-teatree"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-teatree") == Autonomy.FULL.value
        assert "t3-teatree" in result.stdout

    def test_named_overlay_preserves_sibling_overlay(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.BABYSIT.value, scope="t3-client")
        runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "t3-teatree"])
        # The sibling overlay's own value is a distinct row and is preserved ...
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-client") == Autonomy.BABYSIT.value
        # ... while the new overlay gets full.
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-teatree") == Autonomy.FULL.value

    def test_defaults_to_active_overlay(self) -> None:
        """With no --overlay, the value lands in the active overlay's scope (real resolver).

        Overlay discovery is RAW/TOML (#1775 KEEP-TOML): a bare
        ``[overlays.t3-active]`` table is what makes ``t3-active`` the resolvable
        active overlay. The autonomy *value* itself is DB-home, so the write
        lands as an overlay-scoped ``ConfigSetting`` row, not a TOML key.
        """
        config_path = self.tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n[overlays.t3-active]\n", encoding="utf-8")
        self.monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
        self.monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "t3-active")
        result = runner.invoke(_app(), ["autonomy", "set", "full"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-active") == Autonomy.FULL.value

    def test_no_active_overlay_and_no_target_refuses(self) -> None:
        """No --overlay, no --global, and no resolvable active overlay → refuse, write nothing."""
        # No installed overlays and a cwd free of manage.py → no active overlay resolves.
        self.monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
        self.monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        away = self.tmp_path / "no_manage"
        away.mkdir()
        self.monkeypatch.chdir(away)
        result = runner.invoke(_app(), ["autonomy", "set", "full"])
        assert result.exit_code == 1
        assert ConfigSetting.objects.count() == 0


class TestAutonomySetValidation(TestCase):
    def test_typo_is_rejected_and_writes_nothing(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "set", "yolo", "--global"])
        assert result.exit_code == 1
        assert ConfigSetting.objects.count() == 0


class TestAutonomyShow(TestCase):
    def test_show_reports_effective_value(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.NOTIFY.value)
        result = runner.invoke(_app(), ["autonomy", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Autonomy.NOTIFY.value

    def test_show_defaults_to_babysit_when_unset(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Autonomy.BABYSIT.value

    def test_show_is_read_only(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.FULL.value)
        runner.invoke(_app(), ["autonomy", "show"])
        # ``show`` is a pure resolver read — no row is written or cleared.
        assert ConfigSetting.objects.count() == 1
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.FULL.value


@pytest.fixture
def isolated_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Blank the TOML config, blank installed overlays, and a manage.py-free cwd.

    Mirrors the ``config_file`` + ``no_installed_overlays`` + ``elsewhere``
    triple the package-local ``tests/config/conftest.py`` provides — replicated
    here because this module sits at the ``tests/`` root, outside that package.
    The ``autonomy`` value itself is DB-home now (#1775): the staged
    ``~/.teatree.toml`` only carries the TOML-home knobs (``privacy``) a couple
    of cases assert the floor against. Returns the staged config path.
    """
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    away = tmp_path / "no_manage"
    away.mkdir()
    monkeypatch.chdir(away)
    monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
    monkeypatch.delenv("T3_MODE", raising=False)
    return cfg


class TestAutonomyKnobCollapsesGatesNotFloor(TestCase):
    """The knob the CLI persists flips the approval gates, never the safety floor.

    These round-trip through ``get_effective_settings`` after the CLI write so
    the must-ALLOW (autonomous colleague auto-approve + auto-merge) and the
    must-DENY (safety floor stays in force) outcomes are proven end-to-end, not
    just asserted on the raw store.
    """

    @pytest.fixture(autouse=True)
    def _fixtures(self, isolated_resolution: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.isolated_resolution = isolated_resolution
        self.monkeypatch = monkeypatch

    def test_full_must_allow_colleague_autoapprove_and_automerge(self) -> None:
        """``autonomy set full`` collapses on-behalf-post (colleague approve) + merge gates."""
        self.isolated_resolution.write_text("[teatree]\n", encoding="utf-8")
        result = runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "trusted"])
        assert result.exit_code == 0

        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        # must-ALLOW: colleague auto-approve (on-behalf posts publish immediately)
        # and the loop's auto-merge are both unblocked.
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False
        # And the merge-autonomy path is actually reachable (gated on mode == AUTO).
        assert settings.mode is Mode.AUTO

    def test_full_must_deny_relaxing_the_safety_floor(self) -> None:
        """A full-autonomy overlay never relaxes privacy / never-lockout — the floor stays on."""
        self.isolated_resolution.write_text('[teatree]\nprivacy = "strict"\n', encoding="utf-8")
        runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "trusted"])

        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        assert settings.privacy == "strict"
        # never-lockout / self-rescue posture (the orchestrator bash gate) untouched.
        assert settings.orchestrator_bash_gate_enabled is True

    def test_babysit_must_deny_autonomous_merge_and_approve(self) -> None:
        """``autonomy set babysit`` keeps every approval gate blocking (the conservative default)."""
        self.isolated_resolution.write_text("[teatree]\n", encoding="utf-8")
        # ``mode = auto`` is a per-overlay opinion; under the partition it is the
        # overlay-scoped DB row, not a ``[overlays.<name>]`` TOML key.
        ConfigSetting.objects.set_value("mode", Mode.AUTO.value, scope="careful")
        runner.invoke(_app(), ["autonomy", "set", "babysit", "--overlay", "careful"])

        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.BABYSIT
        # must-DENY: even with mode = auto, the gates stay blocking under babysit.
        assert settings.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert settings.require_human_approval_to_merge is True
        assert settings.require_human_approval_to_answer is True


_UNBOOTSTRAPPED_CLI_DRIVER = """
import sys
from typer.testing import CliRunner
import typer
from teatree.cli.autonomy import register_autonomy_commands

app = typer.Typer()
register_autonomy_commands(app)
result = CliRunner().invoke(app, sys.argv[1:])
sys.stdout.write(result.output)
if result.exception is not None and not isinstance(result.exception, SystemExit):
    import traceback
    traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
raise SystemExit(result.exit_code)
"""
"""A subprocess driver that exercises the ``autonomy`` typer commands without
``django.setup()`` — reproducing the real ``t3`` console-script condition
cheaply. It imports ONLY ``teatree.cli.autonomy`` + Typer (not the whole
``teatree.cli`` app tree), so it stays fast, but it still never configures
Django before invoking the command — the exact condition #2622 fires under."""


@pytest.mark.timeout(180)
class TestAutonomySetBootstrapsDjangoInRealProcess:
    """``autonomy set`` / ``show`` work from a process where Django is NOT pre-configured.

    No in-process DB: each case spawns a clean subprocess against its OWN
    isolated ``XDG_DATA_HOME`` SQLite control DB and asserts only on subprocess
    output — so the class needs neither ``TestCase`` nor ``@pytest.mark.django_db``.

    The in-process :class:`~typer.testing.CliRunner` tests above all run inside
    pytest, where ``django.setup()`` has already configured settings — so they
    cannot observe souliane/teatree#2622: the real ``t3`` console-script process
    never runs ``django.setup()`` before dispatching the typer overlay app, so
    the ``set`` body crashed with ``ImproperlyConfigured: Requested setting
    INSTALLED_APPS …`` the moment it touched the ``ConfigSetting`` ORM, and
    ``show`` silently reported the dataclass default (its DB tier fails safe to
    ``{}`` when Django is unconfigured).

    The subprocess invokes ``register_autonomy_commands`` directly in a process
    with no ``DJANGO_SETTINGS_MODULE`` — RED on the unbootstrapped code, GREEN
    once ``set`` delegates to the subprocess seam and ``show`` bootstraps Django.
    """

    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _SRC_ROOT = _REPO_ROOT / "src"

    def _clean_env(self, data_home: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        env["XDG_DATA_HOME"] = str(data_home)
        env["PYTHONPATH"] = os.pathsep.join([str(self._SRC_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        return env

    def _migrate(self, env: dict[str, str]) -> None:
        # The ``config_setting`` write needs the ``ConfigSetting`` table; migrate
        # the isolated control DB once up front (this step DOES configure Django).
        subprocess.run(
            [sys.executable, "-m", "teatree", "migrate", "--no-input"],
            cwd=str(self._REPO_ROOT),
            env={**env, "DJANGO_SETTINGS_MODULE": "teatree.settings"},
            capture_output=True,
            text=True,
            check=True,
        )

    def _autonomy(self, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        """Invoke the ``autonomy`` typer subgroup in an UNbootstrapped subprocess."""
        return subprocess.run(
            [sys.executable, "-c", _UNBOOTSTRAPPED_CLI_DRIVER, "autonomy", *args],
            cwd=str(self._REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_set_global_then_show_round_trips_without_improperly_configured(self, tmp_path: Path) -> None:
        env = self._clean_env(tmp_path / "xdg")
        self._migrate(env)
        result = self._autonomy(env, "set", "notify", "--global")
        combined = result.stdout + result.stderr
        # The bug surfaced as this exact exception class from the ORM touch.
        assert "ImproperlyConfigured" not in combined, combined
        assert "settings are not configured" not in combined, combined
        assert result.returncode == 0, combined
        # And the value actually persisted — round-tripped through ``show`` (which
        # would silently report the ``babysit`` default if it skipped the DB tier).
        shown = self._autonomy(env, "show")
        assert shown.stdout.strip() == Autonomy.NOTIFY.value, shown.stdout + shown.stderr

    def test_set_per_overlay_persists_without_improperly_configured(self, tmp_path: Path) -> None:
        env = self._clean_env(tmp_path / "xdg")
        self._migrate(env)
        result = self._autonomy(env, "set", "notify", "--overlay", "t3-teatree")
        combined = result.stdout + result.stderr
        assert "ImproperlyConfigured" not in combined, combined
        assert result.returncode == 0, combined
