"""Tests for the top-level ``t3 admin`` command (run the Django admin)."""

import sys
from unittest.mock import patch

import typer
from django.contrib.auth import get_user_model
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.admin import _run_server, admin

runner = CliRunner()

_app = typer.Typer()
_app.command()(admin)


def _invoke(*args: str):
    """Invoke ``admin`` with the server launch and browser open stubbed out."""
    with (
        patch("teatree.cli.admin.ensure_django"),
        patch("teatree.cli.admin._ensure_migrated"),
        patch("teatree.cli.admin._run_server") as run_server,
        patch("teatree.cli.admin.webbrowser.open") as browser_open,
    ):
        result = runner.invoke(_app, list(args))
    return result, run_server, browser_open


class AdminSuperuserTestCase(TestCase):
    def test_creates_superuser_when_absent(self) -> None:
        result, _run_server, _browser = _invoke("--no-browser")
        assert result.exit_code == 0
        assert get_user_model().objects.filter(is_superuser=True).exists()

    def test_created_password_is_surfaced(self) -> None:
        result, _run_server, _browser = _invoke("--no-browser")
        assert "created superuser 'admin' with password" in result.output

    def test_honours_admin_user_and_password_env(self) -> None:
        with patch.dict("os.environ", {"T3_ADMIN_USER": "root", "T3_ADMIN_PASSWORD": "s3cret-pw"}):
            result, _run_server, _browser = _invoke("--no-browser")
        assert result.exit_code == 0
        user = get_user_model().objects.get(username="root")
        assert user.is_superuser
        assert user.check_password("s3cret-pw")

    def test_reuses_existing_superuser_without_resetting_password(self) -> None:
        get_user_model().objects.create_superuser(username="existing", password="already-set")
        result, _run_server, _browser = _invoke("--no-browser")
        assert result.exit_code == 0
        assert "using existing superuser 'existing'" in result.output
        assert "password" not in result.output
        assert get_user_model().objects.filter(is_superuser=True).count() == 1
        assert get_user_model().objects.get(username="existing").check_password("already-set")


class AdminServerLaunchTestCase(TestCase):
    def test_launches_server_on_default_host_and_port(self) -> None:
        result, run_server, _browser = _invoke("--no-browser")
        assert result.exit_code == 0
        run_server.assert_called_once_with("127.0.0.1", 8000)

    def test_passes_host_and_port_overrides(self) -> None:
        result, run_server, _browser = _invoke("--no-browser", "--host", "192.168.1.5", "--port", "9001")
        assert result.exit_code == 0
        run_server.assert_called_once_with("192.168.1.5", 9001)


class AdminServerCommandTestCase(TestCase):
    def test_run_server_uses_current_interpreter_not_bare_python(self) -> None:
        # A bare "python" resolves via PATH to whatever shim is first (e.g. a
        # pyenv python with no teatree) → "No module named teatree". The
        # subprocess must use the interpreter running this CLI.
        with patch("teatree.utils.run.run_streamed") as run_streamed:
            _run_server("127.0.0.1", 8000)
        cmd = run_streamed.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:] == ["-m", "teatree", "runserver", "127.0.0.1:8000"]


class AdminBrowserTestCase(TestCase):
    def test_no_browser_flag_suppresses_open(self) -> None:
        result, _run_server, browser_open = _invoke("--no-browser")
        assert result.exit_code == 0
        browser_open.assert_not_called()

    def test_browser_opens_admin_url_by_default(self) -> None:
        # The browser is opened on a short timer so the server can bind first;
        # join the timer so the assertion is deterministic.
        with patch("teatree.cli.admin._BROWSER_OPEN_DELAY_SECONDS", 0):
            result, _run_server, browser_open = _invoke()
        assert result.exit_code == 0
        browser_open.assert_called_once_with("http://127.0.0.1:8000/admin/")
