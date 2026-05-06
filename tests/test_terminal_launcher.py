"""Tests for teatree.agents.terminal_launcher — terminal launch strategies."""

from unittest.mock import MagicMock, patch

import teatree.agents.terminal_launcher as launcher_mod
import teatree.utils.run as utils_run_mod
from teatree.agents.terminal_launcher import (
    LaunchResult,
    detect_available_apps,
    launch,
)


class TestLaunch:
    def test_dispatches_to_ttyd_by_default(self) -> None:
        with patch.object(launcher_mod, "_launch_ttyd", return_value=LaunchResult(mode="ttyd")) as mock:
            result = launch(["echo", "hi"])
        mock.assert_called_once()
        assert result.mode == "ttyd"

    def test_dispatches_to_native_window(self) -> None:
        with patch.object(launcher_mod, "_launch_native_window", return_value=LaunchResult(mode="new-window")) as mock:
            result = launch(["echo", "hi"], mode="new-window")
        mock.assert_called_once()
        assert result.mode == "new-window"

    def test_dispatches_to_native_tab(self) -> None:
        with patch.object(launcher_mod, "_launch_native_tab", return_value=LaunchResult(mode="new-tab")) as mock:
            result = launch(["echo", "hi"], mode="new-tab")
        mock.assert_called_once()
        assert result.mode == "new-tab"


class TestLaunchTtyd:
    def test_returns_empty_when_ttyd_not_found(self) -> None:
        with patch.object(launcher_mod.shutil, "which", return_value=None):
            result = launcher_mod._launch_ttyd(["echo", "hi"])
        assert result.mode == "ttyd"
        assert result.launch_url == ""
        assert result.pid == 0
        assert "ttyd not found" in result.error
        assert "brew install ttyd" in result.error

    def test_launches_ttyd_process(self) -> None:
        mock_proc = MagicMock(pid=12345)
        with (
            patch.object(launcher_mod.shutil, "which", return_value="/usr/bin/ttyd"),
            patch.object(launcher_mod, "find_free_port", return_value=8080),
            patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc),
            patch.object(launcher_mod, "register"),
        ):
            result = launcher_mod._launch_ttyd(["echo", "hi"])
        assert result.launch_url == "http://127.0.0.1:8080"
        assert result.pid == 12345
        assert result.mode == "ttyd"


class TestLaunchNativeWindow:
    def test_dispatches_to_macos_on_darwin(self) -> None:
        with (
            patch.object(launcher_mod.sys, "platform", "darwin"),
            patch.object(
                launcher_mod,
                "_launch_macos_window",
                return_value=LaunchResult(mode="new-window"),
            ) as mock,
        ):
            launcher_mod._launch_native_window(["echo", "hi"], cwd="/tmp", app="terminal")
        mock.assert_called_once_with("echo hi", cwd="/tmp", app="terminal")

    def test_dispatches_to_linux_on_non_darwin(self) -> None:
        with (
            patch.object(launcher_mod.sys, "platform", "linux"),
            patch.object(
                launcher_mod,
                "_launch_linux_window",
                return_value=LaunchResult(mode="new-window"),
            ) as mock,
        ):
            launcher_mod._launch_native_window(["echo", "hi"])
        mock.assert_called_once()


class TestLaunchMacosWindow:
    def test_launches_iterm(self) -> None:
        mock_proc = MagicMock(pid=99)
        with patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc):
            result = launcher_mod._launch_macos_window("echo hi", app="iterm2")
        assert result.pid == 99
        assert result.mode == "new-window"

    def test_launches_terminal_default(self) -> None:
        mock_proc = MagicMock(pid=88)
        with patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc):
            result = launcher_mod._launch_macos_window("echo hi")
        assert result.pid == 88

    def test_includes_cwd(self) -> None:
        mock_proc = MagicMock(pid=77)
        with patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen:
            launcher_mod._launch_macos_window("echo hi", cwd="/work")
        script = mock_popen.call_args[0][0][2]  # osascript -e <script>
        assert "cd /work" in script


class TestLaunchNativeTab:
    def test_dispatches_to_macos_on_darwin(self) -> None:
        with (
            patch.object(launcher_mod.sys, "platform", "darwin"),
            patch.object(
                launcher_mod,
                "_launch_macos_tab",
                return_value=LaunchResult(mode="new-tab"),
            ) as mock,
        ):
            launcher_mod._launch_native_tab(["echo", "hi"], cwd="/tmp", app="iterm2")
        mock.assert_called_once_with("echo hi", cwd="/tmp", app="iterm2")

    def test_falls_back_to_window_on_non_darwin(self) -> None:
        with (
            patch.object(launcher_mod.sys, "platform", "linux"),
            patch.object(
                launcher_mod,
                "_launch_linux_window",
                return_value=LaunchResult(mode="new-window"),
            ) as mock,
        ):
            launcher_mod._launch_native_tab(["echo", "hi"])
        mock.assert_called_once()


class TestLaunchMacosTab:
    def test_launches_iterm_tab(self) -> None:
        mock_proc = MagicMock(pid=101)
        with patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen:
            result = launcher_mod._launch_macos_tab("echo hi", app="iterm2")
        assert result.pid == 101
        assert result.mode == "new-tab"
        script = mock_popen.call_args[0][0][2]
        assert "create tab with default profile" in script
        assert "current window" in script

    def test_launches_terminal_default_via_keystroke(self) -> None:
        mock_proc = MagicMock(pid=102)
        with patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen:
            result = launcher_mod._launch_macos_tab("echo hi")
        assert result.pid == 102
        assert result.mode == "new-tab"
        script = mock_popen.call_args[0][0][2]
        assert "keystroke" in script
        assert "front window" in script

    def test_includes_cwd(self) -> None:
        mock_proc = MagicMock(pid=103)
        with patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen:
            launcher_mod._launch_macos_tab("echo hi", cwd="/work", app="iterm2")
        script = mock_popen.call_args[0][0][2]
        assert "cd /work" in script


class TestLaunchLinuxWindow:
    def test_returns_empty_when_no_terminal(self) -> None:
        with (
            patch.object(launcher_mod.shutil, "which", return_value=None),
            patch.object(launcher_mod, "_detect_linux_terminal", return_value=None),
        ):
            result = launcher_mod._launch_linux_window("echo hi")
        assert result.mode == "new-window"
        assert result.pid == 0
        assert "No terminal emulator found" in result.error

    def test_uses_gnome_terminal(self) -> None:
        mock_proc = MagicMock(pid=55)
        with (
            patch.object(launcher_mod.shutil, "which", return_value="/usr/bin/gnome-terminal"),
            patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen,
        ):
            result = launcher_mod._launch_linux_window("echo hi", app="gnome-terminal")
            assert result.pid == 55
            args = mock_popen.call_args[0][0]
            assert "--" in args

    def test_uses_kitty(self) -> None:
        mock_proc = MagicMock(pid=44)
        with (
            patch.object(launcher_mod.shutil, "which", return_value="/usr/bin/kitty"),
            patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen,
        ):
            result = launcher_mod._launch_linux_window("echo hi", app="kitty")
            assert result.pid == 44
            args = mock_popen.call_args[0][0]
            assert "-e" in args

    def test_uses_generic_terminal(self) -> None:
        mock_proc = MagicMock(pid=33)
        with (
            patch.object(launcher_mod.shutil, "which", return_value="/usr/bin/xterm"),
            patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen,
        ):
            result = launcher_mod._launch_linux_window("echo hi", app="xterm")
            assert result.pid == 33
            args = mock_popen.call_args[0][0]
            assert "-e" in args

    def test_falls_back_to_detect(self) -> None:
        mock_proc = MagicMock(pid=22)
        with (
            patch.object(launcher_mod.shutil, "which", side_effect=[None]),  # app='' not found
            patch.object(launcher_mod, "_detect_linux_terminal", return_value="/usr/bin/xterm"),
            patch.object(utils_run_mod.subprocess, "Popen", return_value=mock_proc),
        ):
            result = launcher_mod._launch_linux_window("echo hi")
        assert result.pid == 22


class TestDetectLinuxTerminal:
    def test_uses_terminal_env_var(self) -> None:
        with (
            patch.dict(launcher_mod.os.environ, {"TERMINAL": "custom-term"}, clear=False),
            patch.object(launcher_mod.shutil, "which", return_value="/usr/bin/custom-term"),
        ):
            result = launcher_mod._detect_linux_terminal()
        assert result == "custom-term"

    def test_falls_back_to_candidates(self) -> None:
        def which_side_effect(name: str) -> str | None:
            return "/usr/bin/kitty" if name == "kitty" else None

        with (
            patch.dict(launcher_mod.os.environ, {}, clear=True),
            patch.object(launcher_mod.shutil, "which", side_effect=which_side_effect),
        ):
            result = launcher_mod._detect_linux_terminal()
        assert result == "/usr/bin/kitty"

    def test_returns_none_when_nothing_found(self) -> None:
        with (
            patch.dict(launcher_mod.os.environ, {}, clear=True),
            patch.object(launcher_mod.shutil, "which", return_value=None),
        ):
            result = launcher_mod._detect_linux_terminal()
        assert result is None


class TestDetectAvailableApps:
    def test_macos_detects_by_app_bundle(self) -> None:
        def mock_which(name: str) -> str | None:
            return "/usr/local/bin/kitty" if name == "kitty" else None

        with (
            patch.object(launcher_mod.sys, "platform", "darwin"),
            patch.object(launcher_mod.shutil, "which", side_effect=mock_which),
            patch("teatree.agents.terminal_launcher.Path") as mock_path_cls,
        ):
            mock_path_cls.return_value.exists.return_value = False
            result = detect_available_apps()
        # kitty found via which, others not found
        assert ("kitty", "kitty") in result

    def test_linux_detects_by_which(self) -> None:
        def which_side_effect(name: str) -> str | None:
            return f"/usr/bin/{name}" if name == "kitty" else None

        with (
            patch.object(launcher_mod.sys, "platform", "linux"),
            patch.object(launcher_mod.shutil, "which", side_effect=which_side_effect),
        ):
            result = detect_available_apps()
        assert ("kitty", "kitty") in result
