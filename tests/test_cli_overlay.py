"""Tests for overlay-related CLI functionality.

Extracted from test_cli.py — covers managepy, uvicorn, overlay app building,
bridge subcommands, overlay command registration, overlay subcommands
(dashboard, resetdb, worker, full_status, start_ticket, ship, daily, agent,
lifecycle), overlay config subcommands, and overlay tool registration.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

import teatree.autostart as autostart_mod
import teatree.cli as cli_mod
import teatree.cli.overlay as cli_overlay_mod
import teatree.cli.review as cli_review_mod
import teatree.config as config_mod
import teatree.core.overlay_loader as overlay_loader_mod
from teatree.cli import _register_overlay_commands
from teatree.cli.overlay import OverlayAppBuilder, managepy, uv_cmd
from teatree.cli.overlay import _uvicorn as _uvicorn_fn

runner = CliRunner()


class TestUvCmd:
    def test_returns_uv_run_command(self, tmp_path):
        result = uv_cmd(tmp_path, "python", "manage.py", "migrate")
        assert result[0].endswith("/uv") or result[0] == "uv"
        assert result[1:3] == ["--directory", str(tmp_path)]
        assert result[3] == "run"
        assert result[4:] == ["python", "manage.py", "migrate"]

    def test_no_extra_args(self, tmp_path):
        result = uv_cmd(tmp_path)
        assert result[1:3] == ["--directory", str(tmp_path)]
        assert result[3] == "run"
        assert len(result) == 4


class TestManagepy:
    def test_none_path_falls_back_to_python_m_teatree(self):
        """managepy(None) falls back to ``python -m teatree``."""
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            managepy(None, "migrate")
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "-m" in cmd
            assert "teatree" in cmd

    def test_no_manage_py_falls_back_to_python_m_teatree(self, tmp_path):
        """Managepy with no manage.py falls back to ``python -m teatree``."""
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            managepy(tmp_path, "migrate")
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "-m" in cmd
            assert "teatree" in cmd

    def test_runs_subprocess(self, tmp_path):
        (tmp_path / "manage.py").write_text("pass\n")
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            managepy(tmp_path, "migrate")
            mock_run.assert_called_once()

    def test_sets_overlay_name_env_var(self, tmp_path):
        """Managepy propagates overlay_name as T3_OVERLAY_NAME in subprocess env."""
        (tmp_path / "manage.py").write_text("pass\n")
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            managepy(tmp_path, "migrate", overlay_name="t3-acme")
            env = mock_run.call_args[1]["env"]
            assert env["T3_OVERLAY_NAME"] == "t3-acme"

    def test_omits_overlay_name_when_empty(self, tmp_path):
        """Managepy does not set T3_OVERLAY_NAME when overlay_name is empty."""
        (tmp_path / "manage.py").write_text("pass\n")
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            managepy(tmp_path, "migrate")
            env = mock_run.call_args[1]["env"]
            assert "T3_OVERLAY_NAME" not in env


class TestUvicorn:
    def test_none_path_falls_back_to_python_m_uvicorn(self):
        """_uvicorn(None, ...) falls back to ``python -m uvicorn``."""
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _uvicorn_fn(None, "127.0.0.1", 8000)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "-m" in cmd
            assert "uvicorn" in cmd

    def test_runs_subprocess_with_project_path(self, tmp_path):
        (tmp_path / "manage.py").write_text("pass\n")
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _uvicorn_fn(tmp_path, "127.0.0.1", 8000, "myapp.settings")
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert Path(call_args[0]).name == "uv"
            assert call_args[1:3] == ["--directory", str(tmp_path)]
            assert "uvicorn" in str(call_args)
            assert "teatree.asgi:application" in str(call_args)

    def test_sets_overlay_name_env_var_in_uvicorn(self):
        with patch.object(cli_overlay_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _uvicorn_fn(None, "127.0.0.1", 8000, overlay_name="t3-acme")
            env = mock_run.call_args[1]["env"]
            assert env["T3_OVERLAY_NAME"] == "t3-acme"


class TestOverlayAppBuilder:
    def test_build_creates_typer_app(self):
        overlay_app = OverlayAppBuilder("test", Path("/tmp/project"), "test.settings").build()
        assert isinstance(overlay_app, typer.Typer)

    def test_bridge_subcommand_registers_command(self):
        builder = OverlayAppBuilder("test", Path("/tmp"), "test.settings")
        group = typer.Typer()
        builder._bridge_subcommand(group, "lifecycle", "setup", "Create worktree")
        # Verify command was registered (Typer stores registered commands internally)
        assert len(group.registered_commands) == 1

    def test_register_commands_with_overlays(self):
        from teatree.config import OverlayEntry  # noqa: PLC0415

        entries = [OverlayEntry(name="test", overlay_class="test.overlay.TestOverlay", project_path=Path("/tmp/test"))]
        active = OverlayEntry(name="test", overlay_class="test.overlay.TestOverlay", project_path=Path("/tmp/test"))

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=active),
            patch.object(config_mod, "discover_overlays", return_value=entries),
        ):
            _register_overlay_commands()

    def test_register_commands_entry_point_overlay_uses_teatree_settings(self):
        from teatree.config import OverlayEntry  # noqa: PLC0415

        entries = [OverlayEntry(name="t3-acme", overlay_class="acme.overlay:AcmeOverlay", project_path=None)]

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch.object(config_mod, "discover_overlays", return_value=entries),
            patch.object(cli_mod, "OverlayAppBuilder") as mock_builder,
        ):
            mock_builder.return_value.build.return_value = typer.Typer()
            _register_overlay_commands()

        mock_builder.assert_called_once_with("t3-acme", None, "teatree.settings")

    def test_register_commands_no_overlays(self):
        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch.object(config_mod, "discover_overlays", return_value=[]),
        ):
            _register_overlay_commands()

    def test_bridge_tool_command_runs_managepy(self, tmp_path):
        """_bridge_tool_command creates a command that delegates to managepy."""
        builder = OverlayAppBuilder("test", tmp_path, "test.settings")
        group = typer.Typer()
        (tmp_path / "manage.py").write_text("pass\n")
        builder._bridge_tool_command(group, "my-tool", "Run my tool", "tool my-tool")

        test_runner = CliRunner()
        with patch.object(cli_overlay_mod, "managepy") as mock_manage:
            result = test_runner.invoke(group, ["my-tool", "extra-arg"])
            assert result.exit_code == 0
            mock_manage.assert_called_once()
            call_args = mock_manage.call_args[0]
            assert call_args[0] == tmp_path
            assert "tool" in call_args
            assert "my-tool" in call_args


class TestOverlayCommands:
    def _mock_active_overlay(self, tmp_path):
        """Return a mock active overlay pointing at tmp_path."""
        return config_mod.OverlayEntry(name="test", overlay_class="test.settings", project_path=tmp_path)

    def _mock_guard(self):
        """Return a context manager that patches DashboardGuard to be a no-op."""
        guard = MagicMock()
        guard.stop_existing.return_value = False
        return patch.object(cli_mod, "DashboardGuard", return_value=guard)

    def test_dashboard(self, tmp_path):
        """Dashboard command migrates and starts uvicorn."""
        from teatree.cli import app  # noqa: PLC0415

        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        with (
            patch.object(cli_mod, "managepy") as mock_manage,
            patch.object(cli_mod, "_uvicorn") as mock_uvicorn,
            patch.object(cli_mod, "subprocess"),
            patch("teatree.cli.discover_active_overlay", return_value=self._mock_active_overlay(tmp_path)),
            patch("socket.socket") as mock_socket_cls,
            self._mock_guard(),
        ):
            mock_sock = MagicMock()
            mock_sock.connect_ex.return_value = 1
            mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = test_runner.invoke(app, ["dashboard"])
            assert result.exit_code == 0
            mock_manage.assert_called_once()
            mock_uvicorn.assert_called_once()

    def test_dashboard_port_in_use(self, tmp_path):
        """Dashboard falls back to a free port when default is in use."""
        import socket as _socket  # noqa: PLC0415

        from teatree.cli import app  # noqa: PLC0415

        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        context_sock = MagicMock()
        context_sock.connect_ex.return_value = 0  # port in use

        ephemeral_sock = MagicMock()
        ephemeral_sock.getsockname.return_value = ("127.0.0.1", 9999)

        call_count = 0

        def socket_factory(family=_socket.AF_INET, type_=_socket.SOCK_STREAM):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                cm = MagicMock()
                cm.__enter__ = MagicMock(return_value=context_sock)
                cm.__exit__ = MagicMock(return_value=False)
                return cm
            return ephemeral_sock

        with (
            patch.object(cli_mod, "managepy"),
            patch.object(cli_mod, "_uvicorn") as mock_uvicorn,
            patch.object(cli_mod, "subprocess"),
            patch("teatree.cli.discover_active_overlay", return_value=self._mock_active_overlay(tmp_path)),
            patch("socket.socket", side_effect=socket_factory),
            self._mock_guard(),
        ):
            result = test_runner.invoke(app, ["dashboard"])
            assert result.exit_code == 0
            assert "Port 8000 in use" in result.output
            assert mock_uvicorn.call_args[0][2] == 9999

    def test_dashboard_stop(self, tmp_path):
        """Dashboard --stop kills existing server and exits."""
        from teatree.cli import app  # noqa: PLC0415

        test_runner = CliRunner()
        guard = MagicMock()
        guard.stop_existing.return_value = True

        with patch.object(cli_mod, "DashboardGuard", return_value=guard):
            result = test_runner.invoke(app, ["dashboard", "--stop"])
            assert result.exit_code == 0
            assert "Dashboard stopped" in result.output
            guard.stop_existing.assert_called_once()

    def test_dashboard_stop_no_server(self, tmp_path):
        """Dashboard --stop reports when no server is running."""
        from teatree.cli import app  # noqa: PLC0415

        test_runner = CliRunner()
        guard = MagicMock()
        guard.stop_existing.return_value = False

        with patch.object(cli_mod, "DashboardGuard", return_value=guard):
            result = test_runner.invoke(app, ["dashboard", "--stop"])
            assert result.exit_code == 0
            assert "No running dashboard" in result.output

    def test_dashboard_kills_existing_on_start(self, tmp_path):
        """Dashboard startup kills any existing server before starting."""
        from teatree.cli import app  # noqa: PLC0415

        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")
        guard = MagicMock()
        guard.stop_existing.return_value = True

        with (
            patch.object(cli_mod, "managepy"),
            patch.object(cli_mod, "_uvicorn"),
            patch.object(cli_mod, "subprocess"),
            patch("teatree.cli.discover_active_overlay", return_value=self._mock_active_overlay(tmp_path)),
            patch("socket.socket") as mock_socket_cls,
            patch.object(cli_mod, "DashboardGuard", return_value=guard),
        ):
            mock_sock = MagicMock()
            mock_sock.connect_ex.return_value = 1
            mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = test_runner.invoke(app, ["dashboard"])
            assert result.exit_code == 0
            guard.stop_existing.assert_called_once()
            guard.write_pid.assert_called_once()


class TestDashboardGuard:
    def test_write_and_read_pid(self, tmp_path):
        """Guard writes and reads current process PID."""
        from teatree.cli import DashboardGuard  # noqa: PLC0415

        pid_file = tmp_path / "dashboard.pid"
        guard = DashboardGuard(pid_file=pid_file)
        guard.write_pid()
        assert pid_file.exists()
        assert pid_file.read_text().strip() == str(os.getpid())

    def test_read_pid_stale(self, tmp_path):
        """Guard cleans up PID file for a dead process."""
        from teatree.cli import DashboardGuard  # noqa: PLC0415

        pid_file = tmp_path / "dashboard.pid"
        pid_file.write_text("99999999")  # unlikely to be a real PID
        guard = DashboardGuard(pid_file=pid_file)
        assert guard._read_pid() is None
        assert not pid_file.exists()

    def test_read_pid_missing(self, tmp_path):
        """Guard returns None when no PID file exists."""
        from teatree.cli import DashboardGuard  # noqa: PLC0415

        guard = DashboardGuard(pid_file=tmp_path / "dashboard.pid")
        assert guard._read_pid() is None

    def test_cleanup(self, tmp_path):
        """Guard removes PID file on cleanup."""
        from teatree.cli import DashboardGuard  # noqa: PLC0415

        pid_file = tmp_path / "dashboard.pid"
        guard = DashboardGuard(pid_file=pid_file)
        guard.write_pid()
        assert pid_file.exists()
        guard.cleanup()
        assert not pid_file.exists()


class TestOverlaySubcommands:
    def test_resetdb(self, tmp_path, monkeypatch):
        """Resetdb deletes DB and migrates."""
        monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path / "data")
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        db_dir = tmp_path / "data" / "test"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "db.sqlite3"
        db_path.write_text("fake db")

        with patch.object(cli_overlay_mod, "managepy"):
            result = test_runner.invoke(overlay_app, ["resetdb"])
            assert result.exit_code == 0
            assert "Deleted" in result.output
            assert "Database recreated" in result.output
            assert not db_path.exists()

    def test_resetdb_no_existing_db(self, tmp_path, monkeypatch):
        """Resetdb works even if DB doesn't exist yet."""
        monkeypatch.setattr("teatree.config.DATA_DIR", tmp_path / "data")
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(cli_overlay_mod, "managepy"):
            result = test_runner.invoke(overlay_app, ["resetdb"])
            assert result.exit_code == 0
            assert "Database recreated" in result.output

    def test_worker_no_project(self):
        """Worker fails when project_path is None."""
        overlay_app = OverlayAppBuilder("test", None, "test.settings").build()
        test_runner = CliRunner()
        result = test_runner.invoke(overlay_app, ["worker"])
        assert result.exit_code == 1
        assert "Cannot find overlay project" in result.output

    def test_worker_starts_processes(self, tmp_path):
        """Worker starts background processes."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(cli_overlay_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen:
            result = test_runner.invoke(overlay_app, ["worker", "--count", "2"])
            assert result.exit_code == 0
            assert mock_popen.call_count == 2
            assert "Started 2 worker(s)" in result.output

    def test_worker_without_overlay_name(self, tmp_path):
        """Worker with empty overlay_name skips T3_OVERLAY_NAME env."""
        overlay_app = OverlayAppBuilder("", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(cli_overlay_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen:
            result = test_runner.invoke(overlay_app, ["worker", "--count", "1"])
            assert result.exit_code == 0
            env = mock_popen.call_args[1]["env"]
            assert "T3_OVERLAY_NAME" not in env

    def test_worker_keyboard_interrupt(self, tmp_path):
        """Worker handles KeyboardInterrupt gracefully."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        mock_proc = MagicMock()
        mock_proc.wait.side_effect = KeyboardInterrupt

        with patch.object(cli_overlay_mod.subprocess, "Popen", return_value=mock_proc):
            result = test_runner.invoke(overlay_app, ["worker", "--count", "1"])
            assert "Shutting down" in result.output
            mock_proc.terminate.assert_called_once()

    def test_full_status(self, tmp_path):
        """full-status delegates to followup refresh."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(cli_overlay_mod, "managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["full-status"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "followup", "refresh", overlay_name="test")

    def test_ship(self, tmp_path):
        """Ship delegates to pr create."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(cli_overlay_mod, "managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["ship", "TICKET-123"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "pr", "create", "TICKET-123", overlay_name="test")

    def test_ship_with_title(self, tmp_path):
        """Ship passes title when specified."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(cli_overlay_mod, "managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["ship", "TICKET-123", "--title", "Fix bug"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(
                tmp_path, "pr", "create", "TICKET-123", "--title", "Fix bug", overlay_name="test"
            )

    def test_daily(self, tmp_path):
        """Daily delegates to followup sync."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(cli_overlay_mod, "managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["daily"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "followup", "sync", overlay_name="test")

    def test_agent(self, tmp_path, monkeypatch):
        """Overlay agent launches claude with overlay context."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        from teatree.cli import doctor as cli_doctor_mod  # noqa: PLC0415
        from teatree.skill_loading import SkillLoadingPolicy, SkillSelectionResult  # noqa: PLC0415

        overlay_obj = MagicMock()
        overlay_obj.metadata.get_skill_metadata.return_value = {"skill_path": "skills/test/SKILL.md"}

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(overlay_loader_mod, "get_overlay", return_value=overlay_obj),
            patch.object(cli_mod, "_detect_agent_ticket_status", return_value="started"),
            patch.object(
                SkillLoadingPolicy,
                "select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["code"]),
            ),
            patch.object(cli_mod.os, "execvp") as mock_exec,
        ):
            test_runner.invoke(overlay_app, ["agent", "fix something"])
            mock_exec.assert_called_once()

    def test_agent_no_project_path(self, tmp_path, monkeypatch):
        """Overlay agent works even with no project_path by falling back to _find_project_root."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        overlay_app = OverlayAppBuilder("test", None, "test.settings").build()
        test_runner = CliRunner()
        from teatree.cli import doctor as cli_doctor_mod  # noqa: PLC0415
        from teatree.skill_loading import SkillLoadingPolicy, SkillSelectionResult  # noqa: PLC0415

        overlay_obj = MagicMock()
        overlay_obj.metadata.get_skill_metadata.return_value = {}

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(cli_doctor_mod.IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(overlay_loader_mod, "get_overlay", return_value=overlay_obj),
            patch.object(cli_mod, "_detect_agent_ticket_status", return_value=""),
            patch.object(
                SkillLoadingPolicy,
                "select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["code"]),
            ),
            patch.object(cli_mod.os, "execvp") as mock_exec,
        ):
            test_runner.invoke(overlay_app, ["agent"])
            mock_exec.assert_called_once()

    def test_agent_rejects_phase_and_skill_together(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        result = test_runner.invoke(overlay_app, ["agent", "--phase", "coding", "--skill", "code"])

        assert result.exit_code == 1
        assert "--phase and --skill cannot be used together." in result.output

    def test_lifecycle_subcommand(self, tmp_path):
        """Overlay command groups forward to manage.py."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(cli_overlay_mod, "managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["lifecycle", "setup"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "lifecycle", "setup", overlay_name="test")

    def test_enable_autostart(self, tmp_path):
        """enable-autostart delegates to teatree.autostart.enable."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with (
            patch.object(config_mod, "discover_active_overlay", return_value=None),
            patch.object(autostart_mod, "enable", return_value="Service installed") as mock_enable,
        ):
            result = test_runner.invoke(overlay_app, ["config", "enable-autostart"])
            assert result.exit_code == 0
            assert "Service installed" in result.output
            mock_enable.assert_called_once()

    def test_disable_autostart(self, tmp_path):
        """disable-autostart delegates to teatree.autostart.disable."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(autostart_mod, "disable", return_value="Service removed") as mock_disable:
            result = test_runner.invoke(overlay_app, ["config", "disable-autostart"])
            assert result.exit_code == 0
            assert "Service removed" in result.output
            mock_disable.assert_called_once_with(overlay_name="test")

    def test_show_logs_stdout(self, tmp_path):
        """Show logs shows stdout log output."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        stdout_log = tmp_path / "stdout.log"
        stdout_log.write_text("log line 1\nlog line 2\n")

        with (
            patch.object(
                autostart_mod,
                "log_paths",
                return_value={"stdout": stdout_log, "stderr": tmp_path / "stderr.log"},
            ),
            patch.object(cli_review_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = test_runner.invoke(overlay_app, ["config", "logs"])
            assert result.exit_code == 0
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "tail" in str(args)

    def test_show_logs_follow(self, tmp_path):
        """Show logs --follow uses tail -f."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        stdout_log = tmp_path / "stdout.log"
        stdout_log.write_text("log data\n")

        with (
            patch.object(
                autostart_mod,
                "log_paths",
                return_value={"stdout": stdout_log, "stderr": tmp_path / "stderr.log"},
            ),
            patch.object(cli_review_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = test_runner.invoke(overlay_app, ["config", "logs", "--follow"])
            assert result.exit_code == 0
            args = mock_run.call_args[0][0]
            assert "-f" in args

    def test_show_logs_no_file(self, tmp_path):
        """Show logs fails when log file doesn't exist."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch.object(
            autostart_mod,
            "log_paths",
            return_value={"stdout": tmp_path / "nonexistent.log", "stderr": tmp_path / "stderr.log"},
        ):
            result = test_runner.invoke(overlay_app, ["config", "logs"])
            assert result.exit_code == 1
            assert "No log file found" in result.output

    def test_show_logs_stderr(self, tmp_path):
        """Show logs --stderr reads the stderr log file."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        stderr_log = tmp_path / "stderr.log"
        stderr_log.write_text("error data\n")

        with (
            patch.object(
                autostart_mod,
                "log_paths",
                return_value={"stdout": tmp_path / "stdout.log", "stderr": stderr_log},
            ),
            patch.object(cli_review_mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = test_runner.invoke(overlay_app, ["config", "logs", "--stderr"])
            assert result.exit_code == 0
            args = mock_run.call_args[0][0]
            assert str(stderr_log) in str(args)

    def test_register_tools_from_json(self, tmp_path):
        """Overlay app registers tool commands from hook-config/tool-commands.json."""
        hook_dir = tmp_path / "skills" / "my-skill" / "hook-config"
        hook_dir.mkdir(parents=True)
        (hook_dir / "tool-commands.json").write_text(
            json.dumps(
                [
                    {"name": "lint", "help": "Run linter", "command": "tool lint"},
                    {"name": "format", "help": "Auto-format code", "command": "tool format"},
                ]
            )
        )

        builder = OverlayAppBuilder("test", tmp_path, "test.settings")
        builder._register_overlay_tools()

        # The tool group should have been registered
        assert len(builder.overlay_app.registered_groups) == 1

    def test_register_tools_skips_entries_without_name(self, tmp_path):
        """_register_overlay_tools skips specs without name or command."""
        hook_dir = tmp_path / "skills" / "my-skill" / "hook-config"
        hook_dir.mkdir(parents=True)
        (hook_dir / "tool-commands.json").write_text(
            json.dumps(
                [
                    {"help": "No name defined"},
                    {"name": "valid", "command": "tool valid", "help": "Works"},
                ]
            )
        )

        builder = OverlayAppBuilder("test", tmp_path, "test.settings")
        builder._register_overlay_tools()

    def test_register_tools_handles_invalid_json(self, tmp_path):
        """_register_overlay_tools skips files with invalid JSON."""
        hook_dir = tmp_path / "skills" / "my-skill" / "hook-config"
        hook_dir.mkdir(parents=True)
        (hook_dir / "tool-commands.json").write_text("not valid json {{{")

        builder = OverlayAppBuilder("test", tmp_path, "test.settings")
        builder._register_overlay_tools()

        # Should not crash, just skip the file
        assert len(builder.overlay_app.registered_groups) == 0

    def test_register_tools_none_path(self):
        """_register_overlay_tools returns early when project_path is None."""
        builder = OverlayAppBuilder("test", None, "test.settings")
        builder._register_overlay_tools()
        assert len(builder.overlay_app.registered_groups) == 0

    def test_register_tools_no_tool_commands(self, tmp_path):
        """_register_overlay_tools returns early when no tool-commands.json found."""
        builder = OverlayAppBuilder("test", tmp_path, "test.settings")
        builder._register_overlay_tools()
        assert len(builder.overlay_app.registered_groups) == 0
