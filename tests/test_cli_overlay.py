"""Tests for overlay-related CLI functionality.

Extracted from test_cli.py — covers managepy, uvicorn, overlay app building,
bridge subcommands, overlay command registration, overlay subcommands
(dashboard, resetdb, worker, full_status, start_ticket, ship, daily, agent,
lifecycle), overlay config subcommands, and overlay tool registration.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from teetree.cli import _register_overlay_commands
from teetree.cli_overlay import OverlayAppBuilder, managepy, uv_cmd
from teetree.cli_overlay import _uvicorn as _uvicorn_fn

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
    def test_none_path(self):
        import click  # noqa: PLC0415

        try:
            managepy(None)
            msg = "Expected Exit"
            raise AssertionError(msg)
        except (SystemExit, click.exceptions.Exit) as e:
            assert e.exit_code == 1  # noqa: PT017

    def test_no_manage_py(self, tmp_path):
        import click  # noqa: PLC0415

        try:
            managepy(tmp_path)
            msg = "Expected Exit"
            raise AssertionError(msg)
        except (SystemExit, click.exceptions.Exit) as e:
            assert e.exit_code == 1  # noqa: PT017

    def test_runs_subprocess(self, tmp_path):
        (tmp_path / "manage.py").write_text("pass\n")
        with patch("teetree.cli_review.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            managepy(tmp_path, "migrate")
            mock_run.assert_called_once()


class TestUvicorn:
    def test_none_path(self):
        import click  # noqa: PLC0415

        try:
            _uvicorn_fn(None, "127.0.0.1", 8000)
            msg = "Expected Exit"
            raise AssertionError(msg)
        except (SystemExit, click.exceptions.Exit) as e:
            assert e.exit_code == 1  # noqa: PT017

    def test_runs_subprocess(self, tmp_path):
        with patch("teetree.cli_overlay.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _uvicorn_fn(tmp_path, "127.0.0.1", 8000, "myapp.settings")
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args[0].endswith("/uv")
            assert call_args[1:3] == ["--directory", str(tmp_path)]
            assert "uvicorn" in str(call_args)
            assert "myapp.asgi:application" in str(call_args)


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
        from teetree.config import OverlayEntry  # noqa: PLC0415

        entries = [OverlayEntry(name="test", settings_module="test.settings", project_path=Path("/tmp/test"))]
        active = OverlayEntry(name="test", settings_module="test.settings", project_path=Path("/tmp/test"))

        with (
            patch("teetree.config.discover_active_overlay", return_value=active),
            patch("teetree.config.discover_overlays", return_value=entries),
        ):
            _register_overlay_commands()

    def test_register_commands_no_overlays(self):
        with (
            patch("teetree.config.discover_active_overlay", return_value=None),
            patch("teetree.config.discover_overlays", return_value=[]),
        ):
            _register_overlay_commands()

    def test_bridge_tool_command_runs_managepy(self, tmp_path):
        """_bridge_tool_command creates a command that delegates to managepy."""
        builder = OverlayAppBuilder("test", tmp_path, "test.settings")
        group = typer.Typer()
        (tmp_path / "manage.py").write_text("pass\n")
        builder._bridge_tool_command(group, "my-tool", "Run my tool", "tool my-tool")

        test_runner = CliRunner()
        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(group, ["my-tool", "extra-arg"])
            assert result.exit_code == 0
            mock_manage.assert_called_once()
            call_args = mock_manage.call_args[0]
            assert call_args[0] == tmp_path
            assert "tool" in call_args
            assert "my-tool" in call_args


class TestOverlayCommands:
    def test_dashboard(self, tmp_path):
        """Dashboard command migrates and starts uvicorn."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        with (
            patch("teetree.cli_overlay.managepy") as mock_manage,
            patch("teetree.cli_overlay._uvicorn") as mock_uvicorn,
            patch("socket.socket") as mock_socket_cls,
        ):
            # Port is free (connect_ex returns non-zero)
            mock_sock = MagicMock()
            mock_sock.connect_ex.return_value = 1
            mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = test_runner.invoke(overlay_app, ["dashboard"])
            assert result.exit_code == 0
            mock_manage.assert_called_once()
            mock_uvicorn.assert_called_once()

    def test_dashboard_port_in_use(self, tmp_path):
        """Dashboard falls back to a free port when default is in use."""
        import socket as _socket  # noqa: PLC0415

        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        # We need to mock socket.socket to return different objects for
        # the context manager socket and the ephemeral socket.
        context_sock = MagicMock()
        context_sock.connect_ex.return_value = 0  # port in use

        ephemeral_sock = MagicMock()
        ephemeral_sock.getsockname.return_value = ("127.0.0.1", 9999)

        call_count = 0

        def socket_factory(family=_socket.AF_INET, type_=_socket.SOCK_STREAM):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # The context manager socket
                cm = MagicMock()
                cm.__enter__ = MagicMock(return_value=context_sock)
                cm.__exit__ = MagicMock(return_value=False)
                return cm
            # The ephemeral socket for finding a free port
            return ephemeral_sock

        with (
            patch("teetree.cli_overlay.managepy"),
            patch("teetree.cli_overlay._uvicorn") as mock_uvicorn,
            patch("socket.socket", side_effect=socket_factory),
        ):
            result = test_runner.invoke(overlay_app, ["dashboard"])
            assert result.exit_code == 0
            assert "Port 8000 in use" in result.output
            # Verify uvicorn was called with the fallback port
            assert mock_uvicorn.call_args[0][2] == 9999

    def test_resetdb(self, tmp_path, monkeypatch):
        """Resetdb deletes DB and migrates."""
        monkeypatch.setattr("teetree.config.DATA_DIR", tmp_path / "data")
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        # Create fake db
        db_dir = tmp_path / "data" / "test"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "db.sqlite3"
        db_path.write_text("fake db")

        with patch("teetree.cli_overlay.managepy"):
            result = test_runner.invoke(overlay_app, ["resetdb"])
            assert result.exit_code == 0
            assert "Deleted" in result.output
            assert "Database recreated" in result.output
            assert not db_path.exists()

    def test_resetdb_no_existing_db(self, tmp_path, monkeypatch):
        """Resetdb works even if DB doesn't exist yet."""
        monkeypatch.setattr("teetree.config.DATA_DIR", tmp_path / "data")
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy"):
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

        with patch("teetree.cli_overlay.subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = test_runner.invoke(overlay_app, ["worker", "--count", "2"])
            assert result.exit_code == 0
            assert mock_popen.call_count == 2
            assert "Started 2 worker(s)" in result.output

    def test_worker_keyboard_interrupt(self, tmp_path):
        """Worker handles KeyboardInterrupt gracefully."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        (tmp_path / "manage.py").write_text("pass\n")

        mock_proc = MagicMock()
        mock_proc.wait.side_effect = KeyboardInterrupt

        with patch("teetree.cli_overlay.subprocess.Popen", return_value=mock_proc):
            result = test_runner.invoke(overlay_app, ["worker", "--count", "1"])
            assert "Shutting down" in result.output
            mock_proc.terminate.assert_called_once()

    def test_full_status(self, tmp_path):
        """full-status delegates to followup refresh."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["full-status"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "followup", "refresh")

    def test_start_ticket(self, tmp_path):
        """start-ticket delegates to workspace ticket."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["start-ticket", "https://issue/123"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "workspace", "ticket", "https://issue/123")

    def test_start_ticket_with_variant(self, tmp_path):
        """start-ticket passes variant when specified."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["start-ticket", "https://issue/123", "--variant", "tenant-a"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(
                tmp_path, "workspace", "ticket", "https://issue/123", "--variant", "tenant-a"
            )

    def test_ship(self, tmp_path):
        """Ship delegates to pr create."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["ship", "TICKET-123"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "pr", "create", "TICKET-123")

    def test_ship_with_title(self, tmp_path):
        """Ship passes title when specified."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["ship", "TICKET-123", "--title", "Fix bug"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "pr", "create", "TICKET-123", "--title", "Fix bug")

    def test_daily(self, tmp_path):
        """Daily delegates to followup sync."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["daily"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "followup", "sync")

    def test_agent(self, tmp_path, monkeypatch):
        """Overlay agent launches claude with overlay context."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()
        from teetree.skill_loading import SkillSelectionResult  # noqa: PLC0415

        overlay_obj = MagicMock()
        overlay_obj.get_skill_metadata.return_value = {"skill_path": "skills/t3-test/SKILL.md"}

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("teetree.cli_doctor.IntrospectionHelpers.editable_info", return_value=(False, "")),
            patch("teetree.core.overlay_loader.get_overlay", return_value=overlay_obj),
            patch("teetree.cli._detect_agent_ticket_status", return_value="started"),
            patch(
                "teetree.skill_loading.SkillLoadingPolicy.select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["t3-code"]),
            ),
            patch("teetree.cli.os.execvp") as mock_exec,
        ):
            test_runner.invoke(overlay_app, ["agent", "fix something"])
            mock_exec.assert_called_once()

    def test_agent_no_project_path(self, tmp_path, monkeypatch):
        """Overlay agent works even with no project_path by falling back to _find_project_root."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        overlay_app = OverlayAppBuilder("test", None, "test.settings").build()
        test_runner = CliRunner()
        from teetree.skill_loading import SkillSelectionResult  # noqa: PLC0415

        overlay_obj = MagicMock()
        overlay_obj.get_skill_metadata.return_value = {}

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("teetree.cli_doctor.IntrospectionHelpers.editable_info", return_value=(False, "")),
            patch("teetree.core.overlay_loader.get_overlay", return_value=overlay_obj),
            patch("teetree.cli._detect_agent_ticket_status", return_value=""),
            patch(
                "teetree.skill_loading.SkillLoadingPolicy.select_for_agent_launch",
                return_value=SkillSelectionResult(skills=["t3-code"]),
            ),
            patch("teetree.cli.os.execvp") as mock_exec,
        ):
            test_runner.invoke(overlay_app, ["agent"])
            mock_exec.assert_called_once()

    def test_agent_rejects_phase_and_skill_together(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        result = test_runner.invoke(overlay_app, ["agent", "--phase", "coding", "--skill", "t3-code"])

        assert result.exit_code == 1
        assert "--phase and --skill cannot be used together." in result.output

    def test_lifecycle_subcommand(self, tmp_path):
        """Overlay command groups forward to manage.py."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.cli_overlay.managepy") as mock_manage:
            result = test_runner.invoke(overlay_app, ["lifecycle", "setup"])
            assert result.exit_code == 0
            mock_manage.assert_called_once_with(tmp_path, "lifecycle", "setup")

    def test_enable_autostart(self, tmp_path):
        """enable-autostart delegates to teetree.autostart.enable."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with (
            patch("teetree.config.discover_active_overlay", return_value=None),
            patch("teetree.autostart.enable", return_value="Service installed") as mock_enable,
        ):
            result = test_runner.invoke(overlay_app, ["config", "enable-autostart"])
            assert result.exit_code == 0
            assert "Service installed" in result.output
            mock_enable.assert_called_once()

    def test_disable_autostart(self, tmp_path):
        """disable-autostart delegates to teetree.autostart.disable."""
        overlay_app = OverlayAppBuilder("test", tmp_path, "test.settings").build()
        test_runner = CliRunner()

        with patch("teetree.autostart.disable", return_value="Service removed") as mock_disable:
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
            patch(
                "teetree.autostart.log_paths",
                return_value={"stdout": stdout_log, "stderr": tmp_path / "stderr.log"},
            ),
            patch("teetree.cli_review.subprocess.run") as mock_run,
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
            patch(
                "teetree.autostart.log_paths",
                return_value={"stdout": stdout_log, "stderr": tmp_path / "stderr.log"},
            ),
            patch("teetree.cli_review.subprocess.run") as mock_run,
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

        with patch(
            "teetree.autostart.log_paths",
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
            patch(
                "teetree.autostart.log_paths",
                return_value={"stdout": tmp_path / "stdout.log", "stderr": stderr_log},
            ),
            patch("teetree.cli_review.subprocess.run") as mock_run,
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
                    {"name": "lint", "help": "Run linter", "management_command": "tool lint"},
                    {"name": "format", "help": "Auto-format code", "management_command": "tool format"},
                ]
            )
        )

        builder = OverlayAppBuilder("test", tmp_path, "test.settings")
        builder._register_overlay_tools()

        # The tool group should have been registered
        assert len(builder.overlay_app.registered_groups) == 1

    def test_register_tools_skips_entries_without_name(self, tmp_path):
        """_register_overlay_tools skips specs without name or management_command."""
        hook_dir = tmp_path / "skills" / "my-skill" / "hook-config"
        hook_dir.mkdir(parents=True)
        (hook_dir / "tool-commands.json").write_text(
            json.dumps(
                [
                    {"help": "No name defined"},
                    {"name": "valid", "management_command": "tool valid", "help": "Works"},
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
