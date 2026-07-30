"""``t3 agent`` bootstraps Django before touching the overlay/config layer.

``t3 agent`` is a plain Typer command reachable before ``django.setup()`` has
run (the ``t3`` console script never configures Django — see
``t3_bootstrap._main``). Its body calls ``discover_active_overlay()`` /
``get_overlay()`` / ``get_effective_settings()``, all of which touch the Django
app registry. Run standalone — from any directory with no ``manage.py`` — the
command therefore crashed with ``django.core.exceptions.ImproperlyConfigured:
Requested setting INSTALLED_APPS, but settings are not configured`` (and its
``AppRegistryNotReady`` sibling once the settings module is set). The command
body must call :func:`teatree.utils.django_bootstrap.ensure_django` before any
overlay/config access, mirroring every other ORM-touching Typer command.

These tests pin the invariant via subprocesses (a clean child interpreter with
``DJANGO_SETTINGS_MODULE`` unset, the pre-bootstrap state a normal shell
invocation starts from) so a future refactor cannot silently re-break it. The
in-process ``tests/test_cli.py`` coverage cannot catch this regression: pytest
already configured Django for the whole test process, so the app registry is
ready there regardless of whether the command bootstraps it.
"""

import os
import subprocess
import sys
from pathlib import Path


def _clean_env() -> dict[str, str]:
    """Return an env without ``DJANGO_SETTINGS_MODULE`` — the pre-bootstrap state.

    Mirrors how the user invokes ``t3 agent`` from a normal shell: no Django
    settings module is pre-exported, the CLI is responsible for setting it.
    """
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return env


class TestAgentCommandBootstrapsDjango:
    """`t3 agent` bootstraps Django before the overlay/config layer runs.

    A child interpreter invokes the command with ``DJANGO_SETTINGS_MODULE``
    unset and from a directory with no ``manage.py``. ``shutil.which`` is
    patched so the ``claude`` binary is always "found" (making the test
    deterministic on hosts with or without Claude Code installed) and
    ``os.execvp`` is patched to a no-op so the real process is not replaced.
    Nothing else is mocked: the command runs the genuine
    ``discover_active_overlay`` / ``get_overlay`` / ``get_effective_settings``
    path, which is exactly what raised ``ImproperlyConfigured`` before the fix.
    """

    def test_agent_does_not_raise_improperly_configured(self, tmp_path: Path) -> None:
        probe = (
            "from unittest.mock import patch\n"
            "from typer.testing import CliRunner\n"
            "import teatree.cli.agent as agent_mod\n"
            "from teatree.cli import app\n"
            "\n"
            "runner = CliRunner()\n"
            "with patch('shutil.which', return_value='/usr/bin/claude'), \\\n"
            "     patch.object(agent_mod.os, 'execvp') as execvp:\n"
            "    result = runner.invoke(app, ['agent', 'fix the sync bug'])\n"
            "if result.exception is not None:\n"
            "    import traceback\n"
            "    traceback.print_exception(\n"
            "        type(result.exception), result.exception, result.exception.__traceback__\n"
            "    )\n"
            "    print('OUTPUT:', result.output)\n"
            "    raise SystemExit(2)\n"
            "assert execvp.called, 'command did not reach the claude launch'\n"
            "assert result.exit_code == 0, result.output\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=_clean_env(),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert "ImproperlyConfigured" not in result.stdout
        assert "ImproperlyConfigured" not in result.stderr
        assert "AppRegistryNotReady" not in result.stdout
        assert "AppRegistryNotReady" not in result.stderr


class TestAgentHelpIsBootstrapSafe:
    """`t3 agent --help` renders without Django configured.

    Help rendering does not touch the ORM, but this is a cheap guard that the
    command entry stays wired into the Typer app and reachable from the
    pre-bootstrap ``t3`` console-script path.
    """

    def test_agent_help_does_not_raise_improperly_configured(self, tmp_path: Path) -> None:
        probe = (
            "from typer.testing import CliRunner\n"
            "from teatree.cli import app\n"
            "\n"
            "result = CliRunner().invoke(app, ['agent', '--help'])\n"
            "assert result.exit_code == 0, result.output\n"
            "assert 'Launch Claude Code' in result.output, result.output\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=_clean_env(),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert "ImproperlyConfigured" not in result.stderr
