import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from typing import cast
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.cli.overlay as cli_overlay_mod
import teatree.core.management.commands.lifecycle as lifecycle_mod
import teatree.core.management.commands.run as run_mod
import teatree.core.models.worktree as worktree_model_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.utils.ports as ports_mod
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.conftest import CommandOverlay

COMMAND_SETTINGS = {
    "TEATREE_TERMINAL_MODE": "same-terminal",
}

_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestRunCommand(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_verify_transitions_to_ready_and_returns_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/20", variant="acme")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)

            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(lifecycle_mod, "Popen") as mock_popen,
                patch.object(
                    run_mod.urllib.request,
                    "urlopen",
                    return_value=mock_resp,
                ),
            ):
                mock_popen.return_value = MagicMock(pid=12345, poll=MagicMock(return_value=None))
                worktree_id = cast("int", call_command("lifecycle", "setup", path=wt_path))
                call_command("lifecycle", "start", path=wt_path)

                result = cast("dict[str, object]", call_command("run", "verify", path=wt_path))

            worktree = Worktree.objects.get(pk=worktree_id)
            assert result["state"] == Worktree.State.READY
            assert isinstance(result["urls"], dict)
            assert worktree.state == Worktree.State.READY

    @override_settings(**COMMAND_SETTINGS)
    def test_verify_does_not_transition_when_endpoint_fails(self) -> None:
        """When HTTP check raises an exception, verify logs the error and does NOT advance FSM."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/30", variant="acme")
            wt = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )

            def _fail_urlopen(*_args: object, **_kwargs: object) -> None:
                msg = "Connection refused"
                raise OSError(msg)

            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(lifecycle_mod, "Popen") as mock_popen,
                patch.object(
                    run_mod.urllib.request,
                    "urlopen",
                    side_effect=_fail_urlopen,
                ),
            ):
                mock_popen.return_value = MagicMock(pid=12345, poll=MagicMock(return_value=None))
                cast("int", call_command("lifecycle", "setup", path=wt_path))
                call_command("lifecycle", "start", path=wt_path)

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
    def test_services_returns_run_commands_from_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/21", variant="acme")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
                cast("int", call_command("lifecycle", "setup", path=wt_path))

                result = cast("dict[str, str]", call_command("run", "services", path=wt_path))

            assert result == {
                "backend": ["run-backend", "/tmp/backend"],
                "frontend": ["run-frontend", "/tmp/backend"],
            }

    def _assert_pre_run_steps(self, service: str) -> None:
        """Pre-run steps are executed before each service command."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(
                overlay="test", issue_url=f"https://example.com/issues/{service}", variant="acme"
            )
            wt = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )
            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(lifecycle_mod, "Popen") as mock_popen,
                patch.object(
                    run_mod.subprocess,
                    "run",
                    side_effect=lambda *a, **kw: CompletedProcess(a[0], 0, "", ""),
                ),
            ):
                mock_popen.return_value = MagicMock(pid=12345, poll=MagicMock(return_value=None))
                cast("int", call_command("lifecycle", "setup", path=wt_path))
                call_command("lifecycle", "start", path=wt_path)

                call_command("run", service, path=wt_path)

            worktree = Worktree.objects.get(pk=wt.pk)
            assert (worktree.extra or {}).get(f"pre_run_{service}") == "ran"

    @override_settings(**COMMAND_SETTINGS)
    def test_executes_pre_run_steps_frontend(self) -> None:
        self._assert_pre_run_steps("frontend")

    @override_settings(**COMMAND_SETTINGS)
    def test_executes_pre_run_steps_backend(self) -> None:
        self._assert_pre_run_steps("backend")

    @override_settings(**COMMAND_SETTINGS)
    def test_executes_pre_run_steps_build_frontend(self) -> None:
        self._assert_pre_run_steps("build-frontend")


class TestCliOverlay:
    @patch.object(cli_overlay_mod.subprocess, "run")
    def test_managepy_calls_uv(self, mock_run: MagicMock, tmp_path: Path) -> None:
        from teatree.cli.overlay import managepy  # noqa: PLC0415

        (tmp_path / "manage.py").write_text("# stub", encoding="utf-8")
        managepy(tmp_path, "migrate", "--no-input")

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0].endswith("/uv")
        assert cmd[1:3] == ["--directory", str(tmp_path)]
        assert cmd[-2:] == ["migrate", "--no-input"]

    @patch.object(cli_overlay_mod.subprocess, "run")
    @patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "acme.settings"})
    def test_uvicorn_launches_asgi_with_reload(self, mock_run: MagicMock, tmp_path: Path) -> None:
        from teatree.cli.overlay import _uvicorn  # noqa: PLC0415

        (tmp_path / "manage.py").write_text("pass\n")
        _uvicorn(tmp_path, "127.0.0.1", 8000)

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0].endswith("/uv")
        assert cmd[1:3] == ["--directory", str(tmp_path)]
        assert "uvicorn" in cmd
        assert "teatree.asgi:application" in cmd
        assert "--host" in cmd
        assert "--reload" in cmd
        assert cmd[cmd.index("--port") + 1] == "8000"
        # _uvicorn sets DJANGO_SETTINGS_MODULE for the subprocess
        call_env = mock_run.call_args[1]["env"]
        assert call_env["DJANGO_SETTINGS_MODULE"] == "teatree.settings"

    @patch.object(cli_overlay_mod.subprocess, "run")
    def test_uvicorn_none_project_path_falls_back(self, mock_run: MagicMock) -> None:
        from teatree.cli.overlay import _uvicorn  # noqa: PLC0415

        mock_run.return_value = MagicMock(returncode=0)
        _uvicorn(None, "127.0.0.1", 8000)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "-m" in cmd
        assert "uvicorn" in cmd


class TestPortPreservation(TestCase):
    @override_settings(**COMMAND_SETTINGS, T3_WORKSPACE_DIR="/tmp/should-not-be-used")
    def test_lifecycle_setup_preserves_already_assigned_ports(self) -> None:
        """Ports that are already assigned are preserved — a running service is expected to hold its port."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"
            worktree_path = workspace / "ac-ticket-42" / "backend"
            worktree_path.mkdir(parents=True)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/42", variant="acme")
            worktree = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(worktree_path)},
            )
            worktree.provision(ports={"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379})
            worktree.save()

            with (
                patch.object(
                    Worktree,
                    "_port_available",
                    staticmethod(lambda port: port not in {8001, 4201, 5433}),
                ),
                patch.object(ports_mod, "port_in_use", side_effect=lambda port: port in {8001, 4201, 5433}),
                patch.object(worktree_model_mod, "_workspace_dir", return_value=Path(str(workspace))),
                patch.object(
                    lifecycle_mod.subprocess,
                    "run",
                    side_effect=lambda *a, **kw: CompletedProcess(a[0], 0, "", ""),
                ),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            ):
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
    def test_run_backend_preserves_ports_before_launch(self) -> None:
        """Already-assigned ports are preserved when running backend — services may be using them."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"
            worktree_path = workspace / "ac-ticket-43" / "backend"
            worktree_path.mkdir(parents=True)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/43", variant="acme")
            worktree = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
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

            commands: list[tuple[object, dict[str, object]]] = []

            def fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
                commands.append((args[0], kwargs))
                return CompletedProcess(args[0], 0, "", "")

            with (
                patch.object(
                    Worktree,
                    "_port_available",
                    staticmethod(lambda port: port not in {8001, 4201, 5433}),
                ),
                patch.object(ports_mod, "port_in_use", side_effect=lambda port: port in {8001, 4201, 5433}),
                patch.object(worktree_model_mod, "_workspace_dir", return_value=Path(str(workspace))),
                patch.object(run_mod.subprocess, "run", side_effect=fake_run),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            ):
                result = cast("str", call_command("run", "backend", path=str(worktree_path)))

            worktree.refresh_from_db()
            assert result == "Backend started."
            assert worktree.ports == {"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379}
            assert commands[-1][1]["check"] is True

    @override_settings(**COMMAND_SETTINGS, T3_WORKSPACE_DIR="/tmp/should-not-be-used")
    def test_run_backend_sets_virtual_env_when_venv_exists(self) -> None:
        """VIRTUAL_ENV is set to the worktree's .venv when it exists on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"
            worktree_path = workspace / "ac-ticket-44" / "backend"
            worktree_path.mkdir(parents=True)
            venv_dir = worktree_path / ".venv"
            venv_dir.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/44", variant="acme")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(worktree_path)},
                state=Worktree.State.PROVISIONED,
                ports={"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379},
                db_name="wt_44_acme",
            )

            envfile = worktree_path.parent / ".env.worktree"
            envfile.write_text("BACKEND_PORT=8001\n", encoding="utf-8")
            (worktree_path / ".env.worktree").symlink_to(envfile)

            captured_envs: list[dict[str, str]] = []

            def fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
                if "env" in kwargs:
                    captured_envs.append(cast("dict[str, str]", kwargs["env"]))
                return CompletedProcess(args[0], 0, "", "")

            with (
                patch.object(
                    Worktree,
                    "_port_available",
                    staticmethod(lambda port: port not in {8001, 4201, 5433}),
                ),
                patch.object(ports_mod, "port_in_use", side_effect=lambda port: port in {8001, 4201, 5433}),
                patch.object(worktree_model_mod, "_workspace_dir", return_value=Path(str(workspace))),
                patch.object(run_mod.subprocess, "run", side_effect=fake_run),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            ):
                call_command("run", "backend", path=str(worktree_path))

            assert captured_envs
            assert captured_envs[-1]["VIRTUAL_ENV"] == str(venv_dir)


class TestE2ePrivateCommand(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_reads_ports_and_variant_from_env_worktree(self) -> None:
        """e2e_private reads FRONTEND_PORT and WT_VARIANT from .env.worktree."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            private_tests_dir = tmp_path / "private-tests"
            private_tests_dir.mkdir()

            worktree_dir = tmp_path / "workspace" / "backend"
            worktree_dir.mkdir(parents=True)
            envfile = worktree_dir / ".env.worktree"
            envfile.write_text(
                "FRONTEND_PORT=4299\nWT_VARIANT=acme\nBACKEND_PORT=8099\n",
                encoding="utf-8",
            )

            captured_envs: list[dict[str, str]] = []

            def fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
                if "env" in kwargs:
                    captured_envs.append(cast("dict[str, str]", kwargs["env"]))
                return CompletedProcess(args[0], 0, "", "")

            with (
                patch.dict(
                    "os.environ",
                    {
                        "T3_PRIVATE_TESTS": str(private_tests_dir),
                        "T3_ORIG_CWD": str(worktree_dir),
                    },
                ),
                patch.object(run_mod.subprocess, "run", side_effect=fake_run),
            ):
                result = cast("str", call_command("run", "e2e-private"))

            assert result == "E2E passed."
            assert captured_envs
            assert captured_envs[-1]["BASE_URL"] == "http://localhost:4299"
            assert captured_envs[-1]["CUSTOMER"] == "acme"

    @override_settings(**COMMAND_SETTINGS)
    def test_uses_default_port_when_no_env_worktree(self) -> None:
        """e2e_private falls back to port 4200 when no .env.worktree exists."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            private_tests_dir = tmp_path / "private-tests"
            private_tests_dir.mkdir()

            # No .env.worktree — should use defaults
            bare_dir = tmp_path / "bare"
            bare_dir.mkdir()

            captured_envs: list[dict[str, str]] = []

            def fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
                if "env" in kwargs:
                    captured_envs.append(cast("dict[str, str]", kwargs["env"]))
                return CompletedProcess(args[0], 0, "", "")

            with (
                patch.dict(
                    "os.environ",
                    {
                        "T3_PRIVATE_TESTS": str(private_tests_dir),
                        "T3_ORIG_CWD": str(bare_dir),
                    },
                ),
                patch.object(run_mod.subprocess, "run", side_effect=fake_run),
            ):
                result = cast("str", call_command("run", "e2e-private"))

            assert result == "E2E passed."
            assert captured_envs
            assert captured_envs[-1]["BASE_URL"] == "http://localhost:4200"
            assert "CUSTOMER" not in captured_envs[-1]
