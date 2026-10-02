"""``t3 setup`` bootstraps Django before the in-process overlay DM provisioning.

The ``run`` callback's self-DB migrate and loop-seed steps run in child
interpreters, but ``provision_all_overlay_dm_channels`` reads the DB
``overlays`` registry — a ``ConfigSetting`` ORM read — in-process. #3074
moved that registry read from ``.teatree.toml`` onto the model, so without a
``django.setup()`` in the command body a plain-shell ``t3 setup`` (and the
``t3 update`` reinstall+setup phase) crashes with ``ImproperlyConfigured:
Requested setting INSTALLED_APPS, but settings are not configured`` the
moment the registry read imports the model.

Both invariants are pinned via child interpreters with
``DJANGO_SETTINGS_MODULE`` unset — the pre-bootstrap state a normal shell
invocation starts from (the technique of
``tests/teatree_cli/test_review_django_bootstrap.py``).
"""

import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from teatree.cli.setup import command as setup_command
from teatree.cli.setup.statusline_installer import StatuslineInstall
from teatree.provisioning.skill_clone_install import CloneInstall
from teatree.provisioning.skills_cli import SkillsCliCommandError

_RUN_SETUP_PROBE = """
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from teatree.cli.setup import command as setup_module
from teatree.config import UserSettings

repo = Path.home() / "teatree-repo"
(repo / ".git").mkdir(parents=True)
(repo / "apm.yml").touch()

ran: list[str] = []


def probe_provision(*, echo):
    from teatree.core.models import ConfigSetting

    ran.append(ConfigSetting.__name__)
    return []


def probe_notion_routing():
    from teatree.core.models import ConfigSetting

    ran.append(f"NotionRoute:{ConfigSetting.__name__}")
    return 1


def probe_notion(echo):
    from teatree.core.models import Ticket

    ran.append(Ticket.__name__)
    return True


config = MagicMock()
config.user = UserSettings()

with (
    patch.object(setup_module, "find_main_clone", return_value=repo),
    patch.object(setup_module, "validate_repo", return_value=repo),
    patch.object(setup_module, "_repair_dep_drift"),
    patch.object(setup_module, "ToolInstaller"),
    patch.object(setup_module, "strip_apm_hooks", return_value=0),
    patch.object(
        setup_module,
        "install_statusline",
        return_value=setup_module.StatuslineInstall.ALREADY_PRESENT,
    ),
    patch.object(setup_module, "agent_skill_dirs", return_value=[]),
    patch.object(setup_module, "ensure_self_db_migrated", return_value=False),
    patch.object(setup_module, "seed_default_loops"),
    patch.object(setup_module, "provision_declared_notion_routing", probe_notion_routing),
    patch.object(setup_module, "provision_all_overlay_dm_channels", probe_provision),
    patch.object(setup_module, "report_notion_connections", probe_notion),
    patch("teatree.config.load_config", return_value=config),
    patch("teatree.config.clone_root", return_value=Path.home() / "workspace"),
    patch("teatree.cli.recommended_authorizations.report_missing_authorizations"),
):
    setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)

assert ran == ["NotionRoute:ConfigSetting", "ConfigSetting", "Ticket"], f"a DB-reading setup step never ran: {ran}"
print("setup-bootstrap-ok")
"""


def _pre_bootstrap_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("XDG_DATA_HOME", None)
    env["HOME"] = str(home)
    return env


class TestReportStatuslineInstallUnwritable:
    """An unwritable settings.json warns and continues — it never aborts setup.

    In the headless container the ``teatree`` user cannot write the root-owned
    ``~/.claude/settings.json``; the installer degrades to
    :attr:`StatuslineInstall.UNWRITABLE`, and the command must echo a WARN and
    return normally so ``t3 setup`` (under ``set -euo pipefail``) exits 0.
    """

    def test_unwritable_warns_and_does_not_raise(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        settings = tmp_path / "settings.json"
        repo = tmp_path / "repo"
        with patch.object(setup_command, "install_statusline", return_value=StatuslineInstall.UNWRITABLE):
            setup_command._report_statusline_install(settings, repo)
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "settings.json" in out


class TestRefreshSkillInventory:
    def test_refreshes_the_explicit_receipt_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        receipt = tmp_path / "inventory.json"
        cli = object()
        seen: list[tuple[Path, object]] = []
        monkeypatch.setattr(
            setup_command,
            "refresh_inventory_receipt",
            lambda path, *, cli: seen.append((path, cli)),
        )

        assert setup_command._refresh_skill_inventory(receipt, cli=cli, echo=lambda _line: None)
        assert seen == [(receipt, cli)]

    def test_refresh_failure_warns_and_returns_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail(_path: Path, *, cli: object) -> None:
            raise SkillsCliCommandError(("skills", "list"), 1, "offline")

        monkeypatch.setattr(setup_command, "refresh_inventory_receipt", fail)
        lines: list[str] = []

        assert not setup_command._refresh_skill_inventory(tmp_path / "inventory.json", cli=object(), echo=lines.append)
        assert any(line.startswith("WARN") and "offline" in line for line in lines)


class TestStrictAgentSkillsSetup:
    def test_unavailable_source_is_unverified_but_installed_demands_are_ready(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        for name in ("required-skill", "cold-review"):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text("# installed\n", encoding="utf-8")
        receipt = tmp_path / "data" / "skills" / "setup-outcome"

        ready = setup_command._assess_dispatched_skills(
            ("required-skill", "cold-review"),
            [CloneInstall(label="private", unavailable="clone inaccessible")],
            receipt=receipt,
            search_dirs=[skills],
        )

        assert ready
        assert receipt.read_text(encoding="utf-8") == "status=ready\nprovenance=unverified\nmissing=\n"
        setup_command._complete_strict_skills_setup(receipt.parent / "ready", strict=True, ready=ready)
        assert (receipt.parent / "ready").read_text(encoding="utf-8") == "v1\n"

    def test_missing_dispatched_skill_refuses_strict_readiness(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / "required-skill").mkdir(parents=True)
        (skills / "required-skill" / "SKILL.md").write_text("# installed\n", encoding="utf-8")
        receipt = tmp_path / "data" / "skills" / "setup-outcome"

        ready = setup_command._assess_dispatched_skills(
            ("required-skill", "cold-review"),
            [CloneInstall(label="private", unavailable="clone inaccessible")],
            receipt=receipt,
            search_dirs=[skills],
        )

        assert not ready
        assert (
            receipt.read_text(encoding="utf-8") == "status=missing-skills\nprovenance=unverified\nmissing=cold-review\n"
        )
        with pytest.raises(typer.Exit):
            setup_command._complete_strict_skills_setup(receipt.parent / "ready", strict=True, ready=ready)
        assert not (receipt.parent / "ready").exists()

    def test_invalid_utf8_skill_body_is_not_claimed_loadable(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / "required-skill").mkdir(parents=True)
        (skills / "required-skill" / "SKILL.md").write_bytes(b"\xff")
        receipt = tmp_path / "data" / "skills" / "setup-outcome"

        ready = setup_command._assess_dispatched_skills(("required-skill",), [], receipt=receipt, search_dirs=[skills])

        assert not ready
        assert "missing=required-skill" in receipt.read_text(encoding="utf-8")

    def test_first_runtime_match_is_invalid_even_when_later_copy_is_healthy(self, tmp_path: Path) -> None:
        first, later = tmp_path / "first", tmp_path / "later"
        for root in (first, later):
            (root / "required-skill").mkdir(parents=True)
        (first / "required-skill" / "SKILL.md").write_bytes(b"\xff")
        (later / "required-skill" / "SKILL.md").write_text("# healthy\n", encoding="utf-8")
        receipt = tmp_path / "setup-outcome"

        assert not setup_command._assess_dispatched_skills(
            ("required-skill",), [], receipt=receipt, search_dirs=[first, later]
        )
        assert "missing=required-skill" in receipt.read_text(encoding="utf-8")

    def test_default_runtime_roots_include_codex_skills(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        codex = tmp_path / ".codex" / "skills"
        (codex / "required-skill").mkdir(parents=True)
        (codex / "required-skill" / "SKILL.md").write_text("# installed\n", encoding="utf-8")
        monkeypatch.setattr(setup_command, "harness_skills_dirs", lambda: [tmp_path / "empty", codex])

        assert setup_command._assess_dispatched_skills(("required-skill",), [], receipt=tmp_path / "setup-outcome")

    def test_path_qualified_runtime_skill_is_accepted(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / "rules").mkdir(parents=True)
        (skills / "rules" / "SKILL.md").write_text("# rules\n", encoding="utf-8")

        assert setup_command._assess_dispatched_skills(
            ("skills/rules/SKILL.md",), [], receipt=tmp_path / "setup-outcome", search_dirs=[skills]
        )

    def test_failure_removes_stale_ready_marker_and_exits_nonzero(self, tmp_path: Path) -> None:
        marker = tmp_path / "skills" / "ready"
        marker.parent.mkdir()
        marker.write_text("v1\n", encoding="utf-8")

        setup_command._reset_strict_skills_marker(marker, strict=True)
        with pytest.raises(typer.Exit) as raised:
            setup_command._complete_strict_skills_setup(marker, strict=True, ready=False)

        assert raised.value.exit_code == 1
        assert not marker.exists()

    def test_success_writes_a_private_ready_marker(self, tmp_path: Path) -> None:
        marker = tmp_path / "skills" / "ready"

        setup_command._complete_strict_skills_setup(marker, strict=True, ready=True)

        assert marker.read_text(encoding="utf-8") == "v1\n"
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600


class TestSetupBootstrapsDjangoBeforeDmProvisioning:
    """The ORM-touching DM-provisioning step must run only after ``django.setup()``.

    A child interpreter drives the ``run`` callback with every heavy
    installer unit patched out and the provisioning step replaced by a probe
    performing the exact move the real ``_load_overlays_registry`` makes
    first: importing ``teatree.core.models.ConfigSetting``. Pre-fix that
    import raises ``ImproperlyConfigured``; the command body owns the
    bootstrap (``ensure_django()``), per ``teatree.utils.django_bootstrap``.
    """

    def test_run_does_not_raise_improperly_configured(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _RUN_SETUP_PROBE],
            check=False,
            capture_output=True,
            text=True,
            env=_pre_bootstrap_env(tmp_path),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert "ImproperlyConfigured" not in result.stderr
        assert "setup-bootstrap-ok" in result.stdout


class TestSetupCommandIsImportSafePreBootstrap:
    """Importing the setup command module must not eager-load the ORM registry.

    The ``t3`` console script imports the full CLI tree before any command
    body runs ``django.setup()`` — a module-scope ``teatree.core.models``
    import anywhere on that path breaks every ``t3`` invocation, including
    ``t3 --help``. The model imports on the setup path stay function-scope.
    """

    def test_module_import_does_not_eager_load_orm_models(self, tmp_path: Path) -> None:
        probe = (
            "import sys\n"
            "import teatree.cli.setup.command\n"
            "assert 'teatree.core.models' not in sys.modules, (\n"
            "    'teatree.cli.setup.command must not eagerly import teatree.core.models — '\n"
            "    'the setup path model imports stay function-scope, after ensure_django()'\n"
            ")\n"
            "print('setup-import-ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            env=_pre_bootstrap_env(tmp_path),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert "setup-import-ok" in result.stdout
