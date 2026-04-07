import subprocess
import sys
from pathlib import Path

import pytest

from teatree.autostart import (
    UnsupportedPlatformError,
    _launchd_plist_path,
    _render_template,
    _resolve_context,
    _systemd_unit_path,
    detect_platform,
    disable,
    enable,
    log_paths,
)


class TestDetectPlatform:
    def test_darwin_returns_launchd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "darwin")
        assert detect_platform() == "launchd"

    def test_linux_returns_systemd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "linux")
        assert detect_platform() == "systemd"

    def test_unsupported_platform_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "win32")
        with pytest.raises(UnsupportedPlatformError, match="win32"):
            detect_platform()


class TestServicePaths:
    def test_launchd_plist_path(self) -> None:
        path = _launchd_plist_path("acme")
        assert path == Path.home() / "Library" / "LaunchAgents" / "com.teatree.acme.dashboard.plist"

    def test_systemd_unit_path(self) -> None:
        path = _systemd_unit_path("acme")
        assert path == Path.home() / ".config" / "systemd" / "user" / "teatree-acme-dashboard.service"


class TestResolveContext:
    def test_uses_venv_python_when_present(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        venv_python = project / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()
        (project / "manage.py").touch()

        ctx = _resolve_context("acme", project, "acme.settings", "127.0.0.1", 8000)

        assert ctx["python"] == str(venv_python)
        assert ctx["manage_py"] == str(project / "manage.py")
        assert ctx["asgi_module"] == "acme.asgi:application"
        assert ctx["host"] == "127.0.0.1"
        assert ctx["port"] == "8000"

    def test_falls_back_to_sys_executable(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "manage.py").touch()

        ctx = _resolve_context("acme", project, "acme.settings", "0.0.0.0", 9000)  # noqa: S104

        assert ctx["python"] == sys.executable


class TestRenderTemplate:
    def test_renders_launchd_template(self) -> None:
        context = {
            "overlay_name": "test",
            "python": "/usr/bin/python3",
            "asgi_module": "test.asgi:application",
            "host": "127.0.0.1",
            "port": "8000",
            "project_path": "/tmp/test",
            "settings_module": "test.settings",
            "stdout_log": "/tmp/out.log",
            "stderr_log": "/tmp/err.log",
            "manage_py": "/tmp/test/manage.py",
        }
        content = _render_template("launchd.plist.tmpl", context)

        assert "com.teatree.test.dashboard" in content
        assert "/usr/bin/python3" in content
        assert "test.asgi:application" in content

    def test_renders_systemd_template(self) -> None:
        context = {
            "overlay_name": "test",
            "python": "/usr/bin/python3",
            "asgi_module": "test.asgi:application",
            "host": "127.0.0.1",
            "port": "8000",
            "project_path": "/tmp/test",
            "settings_module": "test.settings",
            "stdout_log": "/tmp/out.log",
            "stderr_log": "/tmp/err.log",
            "manage_py": "/tmp/test/manage.py",
        }
        content = _render_template("systemd.service.tmpl", context)

        assert "TeaTree Dashboard (test)" in content
        assert "Restart=on-failure" in content
        assert "ExecStartPre=/usr/bin/python3 /tmp/test/manage.py migrate --no-input" in content


class TestEnable:
    def test_launchd_enable_writes_plist_and_loads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "darwin")
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".venv" / "bin").mkdir(parents=True)
        (project / ".venv" / "bin" / "python").touch()
        (project / "manage.py").touch()

        commands_run: list[list[str]] = []
        monkeypatch.setattr(
            "teatree.autostart.subprocess.run",
            lambda *a, **kw: (commands_run.append(a[0]), subprocess.CompletedProcess(a[0], 0))[1],
        )

        msg = enable("acme", project, "acme.settings", "127.0.0.1", 8000)

        plist_path = _launchd_plist_path("acme")
        assert plist_path.is_file()
        assert "com.teatree.acme.dashboard" in plist_path.read_text()
        assert any("launchctl" in str(cmd) for cmd in commands_run)
        assert "127.0.0.1:8000" in msg

    def test_launchd_enable_unloads_existing_before_reinstall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "darwin")
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "manage.py").touch()

        # Pre-create the plist to simulate an existing install
        plist_path = _launchd_plist_path("acme")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text("<plist/>")

        commands_run: list[list[str]] = []
        monkeypatch.setattr(
            "teatree.autostart.subprocess.run",
            lambda *a, **kw: (commands_run.append(a[0]), subprocess.CompletedProcess(a[0], 0))[1],
        )

        enable("acme", project, "acme.settings", "127.0.0.1", 8000)

        # Should have unloaded first, then loaded
        assert len(commands_run) == 2
        assert commands_run[0][1] == "unload"
        assert commands_run[1][1] == "load"

    def test_systemd_enable_writes_unit_and_enables(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "linux")
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "manage.py").touch()

        commands_run: list[list[str]] = []
        monkeypatch.setattr(
            "teatree.autostart.subprocess.run",
            lambda *a, **kw: (commands_run.append(a[0]), subprocess.CompletedProcess(a[0], 0))[1],
        )

        msg = enable("acme", project, "acme.settings", "127.0.0.1", 8000)

        unit_path = _systemd_unit_path("acme")
        assert unit_path.is_file()
        assert "TeaTree Dashboard (acme)" in unit_path.read_text()
        assert any("systemctl" in str(cmd) for cmd in commands_run)
        assert "127.0.0.1:8000" in msg


class TestDisable:
    def test_launchd_disable_unloads_and_removes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "darwin")
        plist_path = _launchd_plist_path("acme")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text("<plist/>")

        commands_run: list[list[str]] = []
        monkeypatch.setattr(
            "teatree.autostart.subprocess.run",
            lambda *a, **kw: (commands_run.append(a[0]), subprocess.CompletedProcess(a[0], 0))[1],
        )

        msg = disable("acme")

        assert not plist_path.exists()
        assert any("launchctl" in str(cmd) for cmd in commands_run)
        assert "removed" in msg.lower()

    def test_systemd_disable_stops_and_removes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "linux")
        unit_path = _systemd_unit_path("acme")
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text("[Service]")

        commands_run: list[list[str]] = []
        monkeypatch.setattr(
            "teatree.autostart.subprocess.run",
            lambda *a, **kw: (commands_run.append(a[0]), subprocess.CompletedProcess(a[0], 0))[1],
        )

        msg = disable("acme")

        assert not unit_path.exists()
        assert any("systemctl" in str(cmd) for cmd in commands_run)
        assert "removed" in msg.lower()

    def test_launchd_disable_noop_when_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "darwin")

        msg = disable("acme")

        assert "not installed" in msg.lower()

    def test_systemd_disable_noop_when_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "linux")

        msg = disable("acme")

        assert "not installed" in msg.lower()


class TestEnableFailures:
    def test_launchd_load_failure_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "darwin")
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "manage.py").touch()

        def fake_run(*a, **kw):
            cmd = a[0]
            if "load" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stderr=b"load error")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("teatree.autostart.subprocess.run", fake_run)

        with pytest.raises(RuntimeError, match="launchctl load failed"):
            enable("acme", project, "acme.settings", "127.0.0.1", 8000)

    def test_systemd_enable_failure_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "linux")
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "manage.py").touch()

        def fake_run(*a, **kw):
            cmd = a[0]
            if "enable" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stderr=b"enable error")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("teatree.autostart.subprocess.run", fake_run)

        with pytest.raises(RuntimeError, match="systemctl enable failed"):
            enable("acme", project, "acme.settings", "127.0.0.1", 8000)

    def test_launchd_unload_failure_logs_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr("teatree.autostart.sys.platform", "darwin")
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "manage.py").touch()

        plist_path = _launchd_plist_path("acme")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text("<plist/>")

        def fake_run(*a, **kw):
            cmd = a[0]
            if "unload" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stderr=b"unload err")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("teatree.autostart.subprocess.run", fake_run)

        with caplog.at_level("WARNING", logger="teatree.autostart"):
            enable("acme", project, "acme.settings", "127.0.0.1", 8000)

        assert "launchctl unload failed" in caplog.text


class TestLogPaths:
    def test_returns_stdout_and_stderr_paths(self) -> None:
        paths = log_paths("acme")

        assert paths["stdout"].name == "dashboard.stdout.log"
        assert paths["stderr"].name == "dashboard.stderr.log"
        assert paths["stdout"].parent == paths["stderr"].parent
