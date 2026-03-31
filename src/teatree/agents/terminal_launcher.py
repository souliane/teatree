"""Terminal launch strategies for interactive agent sessions.

Dispatches to ttyd (browser-based), native window, or native tab
based on ``TEATREE_TERMINAL_MODE``.
"""

import logging
import os
import shutil
import subprocess  # noqa: S404
import sys
from dataclasses import dataclass
from pathlib import Path

from teatree.agents.process_registry import register
from teatree.utils.ports import find_free_port

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LaunchResult:
    launch_url: str = ""
    pid: int = 0
    mode: str = ""


def launch(command: list[str], *, mode: str = "ttyd", cwd: str = "") -> LaunchResult:
    """Launch a command in the configured terminal mode."""
    launchers = {
        "ttyd": _launch_ttyd,
        "browser": _launch_ttyd,
        "new-window": _launch_native_window,
        "new-tab": _launch_native_tab,
    }
    launcher = launchers.get(mode, _launch_ttyd)
    return launcher(command, cwd=cwd)


def _launch_ttyd(command: list[str], *, cwd: str = "") -> LaunchResult:
    ttyd_bin = shutil.which("ttyd")
    if not ttyd_bin:
        logger.error("ttyd not found on PATH")
        return LaunchResult(mode="ttyd")

    port = find_free_port()
    proc = subprocess.Popen(  # noqa: S603
        [ttyd_bin, "--writable", "--port", str(port), "--once", *command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd or None,
    )
    register(proc, f"ttyd (port={port})")

    launch_url = f"http://127.0.0.1:{port}"
    logger.info("Launched ttyd (pid=%d, port=%d)", proc.pid, port)
    return LaunchResult(launch_url=launch_url, pid=proc.pid, mode="ttyd")


def _launch_native_window(command: list[str], *, cwd: str = "") -> LaunchResult:
    cmd_str = " ".join(command)

    if sys.platform == "darwin":
        return _launch_macos_window(cmd_str, cwd=cwd)
    return _launch_linux_window(cmd_str, cwd=cwd)


def _launch_native_tab(command: list[str], *, cwd: str = "") -> LaunchResult:
    term_program = os.environ.get("TERM_PROGRAM", "")
    cmd_str = " ".join(command)

    if sys.platform == "darwin" and term_program == "iTerm.app":
        return _launch_iterm_tab(cmd_str, cwd=cwd)

    # Fall back to new window for unsupported terminals
    return _launch_native_window(command, cwd=cwd)


def _launch_macos_window(cmd_str: str, *, cwd: str = "") -> LaunchResult:
    cd_prefix = f"cd {cwd} && " if cwd else ""
    script = f'tell application "Terminal" to do script "{cd_prefix}{cmd_str}"'
    proc = subprocess.Popen(  # noqa: S603
        ["osascript", "-e", script],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    logger.info("Launched native macOS Terminal window (pid=%d)", proc.pid)
    return LaunchResult(pid=proc.pid, mode="new-window")


def _launch_iterm_tab(cmd_str: str, *, cwd: str = "") -> LaunchResult:
    cd_prefix = f"cd {cwd} && " if cwd else ""
    script = f"""
    tell application "iTerm"
        tell current window
            create tab with default profile
            tell current session
                write text "{cd_prefix}{cmd_str}"
            end tell
        end tell
    end tell
    """
    proc = subprocess.Popen(  # noqa: S603
        ["osascript", "-e", script],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    logger.info("Launched iTerm2 tab (pid=%d)", proc.pid)
    return LaunchResult(pid=proc.pid, mode="new-tab")


def _launch_linux_window(cmd_str: str, *, cwd: str = "") -> LaunchResult:
    terminal = _detect_linux_terminal()
    if not terminal:
        logger.error("No terminal emulator found on PATH")
        return LaunchResult(mode="new-window")

    name = Path(terminal).name
    if name in {"gnome-terminal", "xfce4-terminal"}:
        args = [terminal, "--", "bash", "-c", cmd_str]
    elif name in {"konsole", "kitty", "alacritty"}:
        args = [terminal, "-e", "bash", "-c", cmd_str]
    else:
        args = [terminal, "-e", cmd_str]

    proc = subprocess.Popen(  # noqa: S603
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=cwd or None,
    )
    logger.info("Launched %s window (pid=%d)", name, proc.pid)
    return LaunchResult(pid=proc.pid, mode="new-window")


def _detect_linux_terminal() -> str | None:
    for env_var in ("TERMINAL", "TERM"):
        val = os.environ.get(env_var, "")
        if val and shutil.which(val):
            return val

    for candidate in (
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "kitty",
        "alacritty",
        "xterm",
    ):
        path = shutil.which(candidate)
        if path:
            return path
    return None
