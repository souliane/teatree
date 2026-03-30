from pathlib import Path
from subprocess import CompletedProcess
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from teetree.core.models import Ticket, Worktree

COMMAND_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": "tests.teetree_core.conftest.CommandOverlay",
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}


@pytest.mark.django_db
class TestRunCommand:
    @override_settings(**COMMAND_SETTINGS)
    def test_verify_transitions_to_ready_and_returns_urls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        wt_path = str(wt_dir)
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/20", variant="acme")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": wt_path}
        )
        worktree_id = cast("int", call_command("lifecycle", "setup", path=wt_path))
        call_command("lifecycle", "start", path=wt_path)

        # Mock HTTP health checks to succeed
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr("teetree.core.management.commands.run.urllib.request.urlopen", lambda *a, **k: mock_resp)

        result = cast("dict[str, object]", call_command("run", "verify", path=wt_path))

        worktree = Worktree.objects.get(pk=worktree_id)
        assert result["state"] == Worktree.State.READY
        assert isinstance(result["urls"], dict)
        assert worktree.state == Worktree.State.READY

    @override_settings(**COMMAND_SETTINGS)
    def test_verify_does_not_transition_when_endpoint_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When HTTP check raises an exception, verify logs the error and does NOT advance FSM (lines 53-56)."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        wt_path = str(wt_dir)
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/30", variant="acme")
        wt = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": wt_path}
        )
        cast("int", call_command("lifecycle", "setup", path=wt_path))
        call_command("lifecycle", "start", path=wt_path)

        # Mock HTTP health checks to fail
        def _fail_urlopen(*_args: object, **_kwargs: object) -> None:
            msg = "Connection refused"
            raise OSError(msg)

        monkeypatch.setattr("teetree.core.management.commands.run.urllib.request.urlopen", _fail_urlopen)

        result = cast("dict[str, object]", call_command("run", "verify", path=wt_path))

        worktree = Worktree.objects.get(pk=wt.pk)
        # State should remain SERVICES_UP — not advanced to READY
        assert worktree.state == Worktree.State.SERVICES_UP
        assert result["state"] == Worktree.State.SERVICES_UP
        # Check results contain failure info
        checks = cast("dict[str, dict[str, object]]", result["checks"])
        for check in checks.values():
            assert check["ok"] is False
            assert check["status"] == 0
            assert "Connection refused" in str(check["error"])

    @override_settings(**COMMAND_SETTINGS)
    def test_services_returns_run_commands_from_overlay(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        wt_path = str(wt_dir)
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/21", variant="acme")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": wt_path}
        )
        cast("int", call_command("lifecycle", "setup", path=wt_path))

        result = cast("dict[str, str]", call_command("run", "services", path=wt_path))

        assert result == {
            "backend": "run-backend /tmp/backend",
            "frontend": "run-frontend /tmp/backend",
        }

    @override_settings(**COMMAND_SETTINGS)
    @pytest.mark.parametrize("service", ["frontend", "backend", "build-frontend"])
    def test_executes_pre_run_steps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str) -> None:
        """Pre-run steps are executed before each service command."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        wt_path = str(wt_dir)
        ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{service}", variant="acme")
        wt = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": wt_path}
        )
        cast("int", call_command("lifecycle", "setup", path=wt_path))
        call_command("lifecycle", "start", path=wt_path)

        monkeypatch.setattr(
            "teetree.core.management.commands.run.subprocess.run",
            lambda *a, **kw: CompletedProcess(a[0], 0, "", ""),
        )

        call_command("run", service, path=wt_path)

        worktree = Worktree.objects.get(pk=wt.pk)
        assert (worktree.extra or {}).get(f"pre_run_{service}") == "ran"


class TestCliOverlay:
    @patch("teetree.cli_overlay.subprocess.run")
    def test_managepy_calls_uv(self, mock_run: MagicMock, tmp_path: Path) -> None:
        from teetree.cli_overlay import managepy  # noqa: PLC0415

        (tmp_path / "manage.py").write_text("# stub", encoding="utf-8")
        managepy(tmp_path, "migrate", "--no-input")

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0].endswith("/uv")
        assert cmd[1:3] == ["--directory", str(tmp_path)]
        assert cmd[-2:] == ["migrate", "--no-input"]

    @patch("teetree.cli_overlay.subprocess.run")
    @patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "acme.settings"})
    def test_uvicorn_launches_asgi_with_reload(self, mock_run: MagicMock, tmp_path: Path) -> None:
        from teetree.cli_overlay import _uvicorn  # noqa: PLC0415

        _uvicorn(tmp_path, "127.0.0.1", 8000)

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0].endswith("/uv")
        assert cmd[1:3] == ["--directory", str(tmp_path)]
        assert "uvicorn" in cmd
        assert "acme.asgi:application" in cmd
        assert "--host" in cmd
        assert "--reload" in cmd
        assert cmd[cmd.index("--port") + 1] == "8000"
        # DJANGO_SETTINGS_MODULE should be stripped from env
        call_env = mock_run.call_args[1]["env"]
        assert "DJANGO_SETTINGS_MODULE" not in call_env

    @patch("teetree.cli.subprocess.run")
    def test_uvicorn_none_project_path_exits(self, mock_run: MagicMock) -> None:
        import click  # noqa: PLC0415

        from teetree.cli_overlay import _uvicorn  # noqa: PLC0415

        with pytest.raises(click.exceptions.Exit):
            _uvicorn(None, "127.0.0.1", 8000)
        mock_run.assert_not_called()


@pytest.mark.django_db
class TestPortPreservation:
    @override_settings(**COMMAND_SETTINGS, T3_WORKSPACE_DIR="/tmp/should-not-be-used")
    def test_lifecycle_setup_preserves_already_assigned_ports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ports that are already assigned are preserved — a running service is expected to hold its port."""
        workspace = tmp_path / "workspace"
        worktree_path = workspace / "ac-ticket-42" / "backend"
        worktree_path.mkdir(parents=True)

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/42", variant="acme")
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": str(worktree_path)},
        )
        worktree.provision(ports={"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379})
        worktree.save()

        monkeypatch.setattr(
            Worktree,
            "_port_available",
            staticmethod(lambda port: port not in {8001, 4201, 5433}),
        )
        monkeypatch.setattr("teetree.utils.ports.port_in_use", lambda port: port in {8001, 4201, 5433})
        monkeypatch.setattr("teetree.core.models.settings.T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.setattr(
            "teetree.core.management.commands.lifecycle.subprocess.run",
            lambda *a, **kw: CompletedProcess(a[0], 0, "", ""),
        )

        call_command("lifecycle", "setup", path=str(worktree_path))

        worktree.refresh_from_db()
        # Ports stay as-is — the worktree's own services may be using them
        assert worktree.ports == {"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379}

        envfile = worktree_path.parent / ".env.worktree"
        env_text = envfile.read_text(encoding="utf-8")
        assert "BACKEND_PORT=8001" in env_text
        assert "FRONTEND_PORT=4201" in env_text
        assert "POSTGRES_PORT=5433" in env_text

    @override_settings(**COMMAND_SETTINGS, T3_WORKSPACE_DIR="/tmp/should-not-be-used")
    def test_run_backend_preserves_ports_before_launch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Already-assigned ports are preserved when running backend — services may be using them."""
        workspace = tmp_path / "workspace"
        worktree_path = workspace / "ac-ticket-43" / "backend"
        worktree_path.mkdir(parents=True)

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/43", variant="acme")
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(worktree_path)},
            state=Worktree.State.PROVISIONED,
            ports={"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379},
            db_name="wt_43_acme",
        )

        envfile = worktree_path.parent / ".env.worktree"
        envfile.write_text("BACKEND_PORT=8001\n", encoding="utf-8")
        (worktree_path / ".env.worktree").symlink_to(envfile)

        monkeypatch.setattr(
            Worktree,
            "_port_available",
            staticmethod(lambda port: port not in {8001, 4201, 5433}),
        )
        monkeypatch.setattr("teetree.utils.ports.port_in_use", lambda port: port in {8001, 4201, 5433})
        monkeypatch.setattr("teetree.core.models.settings.T3_WORKSPACE_DIR", str(workspace))

        commands: list[tuple[object, dict[str, object]]] = []

        def fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
            commands.append((args[0], kwargs))
            return CompletedProcess(args[0], 0, "", "")

        monkeypatch.setattr("teetree.core.management.commands.run.subprocess.run", fake_run)

        result = cast("str", call_command("run", "backend", path=str(worktree_path)))

        worktree.refresh_from_db()
        assert result == "Backend started."
        assert worktree.ports == {"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379}
        assert commands[-1][1]["check"] is True
