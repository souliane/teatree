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
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.setup import command as setup_command
from teatree.cli.setup.statusline_installer import StatuslineInstall

_RUN_SETUP_PROBE = """
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from teatree.cli.setup import command as setup_module

repo = Path.home() / "teatree-repo"
(repo / ".git").mkdir(parents=True)
(repo / "apm.yml").touch()

ran: list[str] = []


def probe_provision(*, echo):
    from teatree.core.models import ConfigSetting

    ran.append(ConfigSetting.__name__)
    return []


config = MagicMock()
config.user.excluded_skills = []

with (
    patch.object(setup_module, "find_main_clone", return_value=repo),
    patch.object(setup_module, "validate_repo", return_value=repo),
    patch.object(setup_module, "_repair_dep_drift"),
    patch.object(setup_module, "ToolInstaller"),
    patch.object(setup_module, "ApmInstaller"),
    patch.object(setup_module, "strip_apm_hooks", return_value=0),
    patch.object(
        setup_module,
        "install_statusline",
        return_value=setup_module.StatuslineInstall.ALREADY_PRESENT,
    ),
    patch.object(setup_module, "agent_skill_dirs", return_value=[]),
    patch.object(setup_module, "ensure_self_db_migrated", return_value=False),
    patch.object(setup_module, "seed_default_loops"),
    patch.object(setup_module, "provision_all_overlay_dm_channels", probe_provision),
    patch("teatree.config.load_config", return_value=config),
    patch("teatree.config.clone_root", return_value=Path.home() / "workspace"),
    patch("teatree.cli.recommended_authorizations.report_missing_authorizations"),
):
    setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)

assert ran == ["ConfigSetting"], f"provisioning step never ran: {ran}"
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
