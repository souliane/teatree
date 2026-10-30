"""Tests for the top-level ``t3 admin`` command (run the Django admin)."""

import os
import stat
import sys
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import typer
from django.contrib.auth import get_user_model
from django.test import TestCase
from typer.main import get_command
from typer.testing import CliRunner

from teatree.cli import app as cli_app
from teatree.cli.admin import BROWSE_URL_FILE, DASHBOARD_PATH, _run_server, admin, dashboard, serving_here
from teatree.utils.loopback_forward import FORWARD_PORT, ForwardResult

runner = CliRunner()

_app = typer.Typer()
_app.command()(admin)

_dashboard_app = typer.Typer()
_dashboard_app.command()(dashboard)


def _invoke(*args: str, forward: ForwardResult | None = None, target: typer.Typer | None = None):
    """Invoke the command with the server launch, browser open and HOST FORWARD stubbed out.

    ``ensure_admin_forward`` shells out to ``docker ps``, so leaving it live makes
    every assertion here depend on whether this box happens to be running the
    stack — the URL becomes the 8803 forward instead of the bound host:port.
    """
    with (
        patch("teatree.cli.admin.ensure_django"),
        patch("teatree.cli.admin._ensure_migrated"),
        patch("teatree.cli.admin._collectstatic"),
        # The serving venue, pinned: under CI the suite itself runs in a container, where
        # the command would otherwise take the operator-CLI path and serve nothing.
        patch("teatree.cli.admin.serving_here", return_value=True),
        patch("teatree.cli.admin.ensure_admin_forward", return_value=forward or ForwardResult()),
        patch("teatree.cli.admin._run_server") as run_server,
        patch("teatree.cli.admin.webbrowser.open") as browser_open,
    ):
        result = runner.invoke(target or _app, list(args))
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

    def test_empty_admin_user_falls_back_to_default(self) -> None:
        # The deploy workflow writes T3_ADMIN_USER= (empty) for an unset secret;
        # an empty value must fall back to the default, not crash create_superuser.
        with patch.dict("os.environ", {"T3_ADMIN_USER": "", "T3_ADMIN_PASSWORD": "s3cret-pw"}):
            result, _run_server, _browser = _invoke("--no-browser")
        assert result.exit_code == 0
        user = get_user_model().objects.get(username="admin")
        assert user.is_superuser
        assert user.check_password("s3cret-pw")

    def test_reuses_existing_superuser_without_resetting_password(self) -> None:
        get_user_model().objects.create_superuser(username="existing", email="", password="already-set")
        result, _run_server, _browser = _invoke("--no-browser")
        assert result.exit_code == 0
        assert "using existing superuser 'existing'" in result.output
        assert "password" not in result.output
        assert get_user_model().objects.filter(is_superuser=True).count() == 1
        assert get_user_model().objects.get(username="existing").check_password("already-set")


class AdminCollectStaticTestCase(TestCase):
    def test_admin_collects_static_before_serving(self) -> None:
        # BLOCKING #2: under gunicorn with DEBUG off WhiteNoise serves STATIC_ROOT,
        # which must be populated first — so the admin boot path runs collectstatic.
        with (
            patch("teatree.cli.admin.ensure_django"),
            patch("teatree.cli.admin._ensure_migrated"),
            patch("teatree.cli.admin.serving_here", return_value=True),
            patch("teatree.cli.admin.ensure_admin_forward", return_value=ForwardResult()),
            patch("teatree.cli.admin._run_server"),
            patch("teatree.cli.admin.webbrowser.open"),
            patch("teatree.cli.admin._collectstatic") as collectstatic,
        ):
            result = runner.invoke(_app, ["--no-browser"])
        assert result.exit_code == 0
        collectstatic.assert_called_once_with()


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
    def test_run_server_execs_gunicorn_against_wsgi_app(self) -> None:
        # A production WSGI server (gunicorn) against teatree's WSGI app, not
        # Django's dev runserver. sys.executable -m gunicorn pins the tool-venv
        # interpreter that has teatree + gunicorn (a bare "gunicorn" shim could
        # resolve to a different env with no teatree).
        with patch("teatree.utils.run.run_streamed") as run_streamed:
            _run_server("127.0.0.1", 8000)
        cmd = run_streamed.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:4] == ["-m", "gunicorn", "teatree.wsgi:application"]
        assert "--bind" in cmd
        assert cmd[cmd.index("--bind") + 1] == "127.0.0.1:8000"

    def test_run_server_binds_host_and_port_overrides(self) -> None:
        with patch("teatree.utils.run.run_streamed") as run_streamed:
            _run_server("192.168.1.5", 9001)
        cmd = run_streamed.call_args.args[0]
        assert cmd[cmd.index("--bind") + 1] == "192.168.1.5:9001"


class AdminBrowserTestCase(TestCase):
    def test_browser_receives_a_single_use_login_link(self) -> None:
        with patch("teatree.cli.admin._BROWSER_OPEN_DELAY_SECONDS", 0):
            result, _run_server, browser_open = _invoke()

        assert result.exit_code == 0
        url = browser_open.call_args.args[0]
        assert url.startswith("http://127.0.0.1:8000/login/local/")
        assert self.client.get(urlsplit(url).path).headers["Location"] == "/admin/"

    def test_multiple_operators_open_sign_in_without_selecting_an_account(self) -> None:
        users = get_user_model().objects
        users.create_superuser(username="first", email="", password="unused")
        users.create_superuser(username="second", email="", password="unused")
        with patch("teatree.cli.admin._BROWSER_OPEN_DELAY_SECONDS", 0):
            result, _run_server, browser_open = _invoke(target=_dashboard_app)

        assert result.exit_code == 0
        url = urlsplit(browser_open.call_args.args[0])
        assert url.path == "/login/"
        assert parse_qs(url.query) == {"next": ["/dash/board/"]}

    def test_oauth_sign_in_keeps_the_requested_dashboard_destination(self) -> None:
        response = self.client.get("/login/?next=/dash/board/")

        assert response.status_code == 200
        assert b"/login/oauth/github/start/?next=/dash/board/" in response.content
        assert b"/login/oauth/gitlab/start/?next=/dash/board/" in response.content

    def test_no_browser_flag_suppresses_open(self) -> None:
        result, _run_server, browser_open = _invoke("--no-browser")
        assert result.exit_code == 0
        browser_open.assert_not_called()

    def test_admin_service_does_not_print_a_login_link_to_logs(self) -> None:
        with patch.dict("os.environ", {"TEATREE_ROLE": "admin"}):
            result, _run_server, browser_open = _invoke("--no-browser")
        assert result.exit_code == 0
        assert "login/local" not in result.output
        browser_open.assert_not_called()


class AdminHostForwardTestCase(TestCase):
    """The forward is stubbed everywhere else, so its two branches are covered here."""

    def test_browser_opens_the_forward_url_when_one_is_established(self) -> None:
        with patch("teatree.cli.admin._BROWSER_OPEN_DELAY_SECONDS", 0):
            result, _run_server, browser_open = _invoke(forward=ForwardResult(url="http://127.0.0.1:8803"))
        assert result.exit_code == 0
        url = browser_open.call_args.args[0]
        assert url.startswith("http://127.0.0.1:8803/login/local/")
        assert self.client.get(urlsplit(url).path).headers["Location"] == "/admin/"

    def test_a_forward_that_could_not_be_established_is_reported(self) -> None:
        result, _run_server, _browser = _invoke("--no-browser", forward=ForwardResult(error="the stack is not up"))
        assert result.exit_code == 0
        assert "no loopback forward: the stack is not up" in result.output


class DashboardCommandTestCase(TestCase):
    def test_dashboard_is_registered_as_a_top_level_command(self) -> None:
        assert "dashboard" in get_command(cli_app).commands

    def test_dashboard_opens_the_board_path_not_the_admin(self) -> None:
        with patch("teatree.cli.admin._BROWSER_OPEN_DELAY_SECONDS", 0):
            result, _run_server, browser_open = _invoke(target=_dashboard_app)
        assert result.exit_code == 0
        url = browser_open.call_args.args[0]
        assert url.startswith("http://127.0.0.1:8000/login/local/")
        assert self.client.get(urlsplit(url).path).headers["Location"] == DASHBOARD_PATH

    def test_dashboard_serves_on_loopback_like_the_admin(self) -> None:
        # Same `_serve`, so the dashboard inherits the admin's session boundary.
        result, run_server, _browser = _invoke("--no-browser", target=_dashboard_app)
        assert result.exit_code == 0
        run_server.assert_called_once_with("127.0.0.1", 8000)


def _invoke_containerized(*args: str, forward: ForwardResult, target: typer.Typer | None = None, data_dir: Path):
    """Invoke the command as the CONTAINERIZED operator CLI, with the stack already serving."""
    with (
        patch("teatree.cli.admin.ensure_django"),
        patch("teatree.cli.admin.running_in_container", return_value=True),
        patch("teatree.cli.admin.data_dir_root", return_value=data_dir),
        patch("teatree.cli.admin.ensure_admin_forward", return_value=forward) as forward_call,
        patch("teatree.cli.admin._ensure_migrated") as migrated,
        patch("teatree.cli.admin._run_server") as run_server,
        patch("teatree.cli.admin.webbrowser.open") as browser_open,
        patch.dict("os.environ", {}, clear=False),
    ):
        os.environ.pop("TEATREE_ROLE", None)
        result = runner.invoke(target or _dashboard_app, list(args))
    return result, run_server, browser_open, forward_call, migrated


class ContainerizedOperatorCliTestCase(TestCase):
    """`t3 dashboard` through deploy/t3 runs IN a container, where it must not serve.

    The stack's teatree-admin is already serving on the container's own loopback. A
    second gunicorn there reaches no browser and collides with the first — and a URL
    printed with `http://127.0.0.1:8000/` is a CONTAINER address the
    host resolves to its own empty loopback. That is the ERR_CONNECTION_REFUSED.
    """

    def setUp(self) -> None:
        self.data_dir = Path(mkdtemp())
        self.addCleanup(rmtree, self.data_dir, ignore_errors=True)
        self.reachable = ForwardResult(url="http://127.0.0.1:8803")

    def test_it_never_hands_the_host_the_containers_own_loopback(self) -> None:
        result, *_ = _invoke_containerized(forward=self.reachable, data_dir=self.data_dir)
        assert result.exit_code == 0
        assert "127.0.0.1:8000" not in result.output
        assert "http://127.0.0.1:8803/login/local/" in result.output

    def test_it_does_not_start_a_second_gunicorn(self) -> None:
        _result, run_server, _browser, _forward, migrated = _invoke_containerized(
            forward=self.reachable, data_dir=self.data_dir
        )
        run_server.assert_not_called()
        migrated.assert_not_called()

    def test_it_records_the_resolved_url_for_the_host_wrapper(self) -> None:
        _result, *_ = _invoke_containerized(forward=self.reachable, data_dir=self.data_dir)
        target = self.data_dir / BROWSE_URL_FILE
        recorded = target.read_text(encoding="utf-8").strip()
        assert recorded.startswith("http://127.0.0.1:8803/login/local/")
        assert self.client.get(urlsplit(recorded).path).headers["Location"] == DASHBOARD_PATH
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_the_port_flag_selects_the_published_host_port(self) -> None:
        _result, _run, _browser, forward_call, _migrated = _invoke_containerized(
            "--port", "9111", forward=ForwardResult(url="http://127.0.0.1:9111"), data_dir=self.data_dir
        )
        assert forward_call.call_args.kwargs["port"] == 9111

    def test_an_unset_port_uses_the_documented_forward_port(self) -> None:
        _result, _run, _browser, forward_call, _migrated = _invoke_containerized(
            forward=self.reachable, data_dir=self.data_dir
        )
        assert forward_call.call_args.kwargs["port"] == FORWARD_PORT

    def test_an_unreachable_admin_fails_loud_and_records_nothing(self) -> None:
        result, _run, _browser, _forward, _migrated = _invoke_containerized(
            forward=ForwardResult(error="no running teatree-admin container"), data_dir=self.data_dir
        )
        assert result.exit_code == 1
        assert "cannot reach the admin from the host" in result.output
        assert (self.data_dir / BROWSE_URL_FILE).read_text(encoding="utf-8").strip() == ""


class ServingVenueTestCase(TestCase):
    def test_a_native_host_serves(self) -> None:
        with patch("teatree.cli.admin.running_in_container", return_value=False):
            assert serving_here() is True

    def test_the_admin_role_container_serves(self) -> None:
        with (
            patch("teatree.cli.admin.running_in_container", return_value=True),
            patch.dict("os.environ", {"TEATREE_ROLE": "admin"}),
        ):
            assert serving_here() is True

    def test_the_operator_cli_container_does_not(self) -> None:
        with (
            patch("teatree.cli.admin.running_in_container", return_value=True),
            patch.dict("os.environ", {"TEATREE_ROLE": "worker"}),
        ):
            assert serving_here() is False
