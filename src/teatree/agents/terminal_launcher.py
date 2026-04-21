"""Terminal launch strategies for interactive agent sessions.

Dispatches to ttyd (browser-based), native window, or native tab
based on ``TEATREE_TERMINAL_MODE``.
"""

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from teatree.agents.process_registry import register
from teatree.utils.ports import find_free_port
from teatree.utils.run import DEVNULL, PIPE, spawn

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LaunchResult:
    launch_url: str = ""
    pid: int = 0
    mode: str = ""


def launch(command: list[str], *, mode: str = "ttyd", cwd: str = "", app: str = "") -> LaunchResult:
    """Launch a command in the configured terminal mode."""
    if mode == "new-window":
        return _launch_native_window(command, cwd=cwd, app=app)
    return _launch_ttyd(command, cwd=cwd)


def _launch_ttyd(command: list[str], *, cwd: str = "") -> LaunchResult:
    ttyd_bin = shutil.which("ttyd")
    if not ttyd_bin:
        logger.error("ttyd not found on PATH")
        return LaunchResult(mode="ttyd")

    port = find_free_port()
    proc = spawn(
        [ttyd_bin, "--writable", "--port", str(port), "--once", *command],
        stdout=DEVNULL,
        stderr=PIPE,
        cwd=cwd or None,
    )
    register(proc, f"ttyd (port={port})")

    launch_url = f"http://127.0.0.1:{port}"
    logger.info("Launched ttyd (pid=%d, port=%d)", proc.pid, port)
    return LaunchResult(launch_url=launch_url, pid=proc.pid, mode="ttyd")


def _launch_native_window(command: list[str], *, cwd: str = "", app: str = "") -> LaunchResult:
    cmd_str = " ".join(command)

    if sys.platform == "darwin":
        return _launch_macos_window(cmd_str, cwd=cwd, app=app)
    return _launch_linux_window(cmd_str, cwd=cwd, app=app)


_MACOS_CANDIDATES = [
    ("iterm2", "iTerm"),
    ("terminal", "Terminal"),
    ("kitty", "kitty"),
    ("alacritty", "Alacritty"),
    ("wezterm", "WezTerm"),
]

_LINUX_CANDIDATES = [
    ("gnome-terminal", "GNOME Terminal"),
    ("konsole", "Konsole"),
    ("xfce4-terminal", "Xfce Terminal"),
    ("kitty", "kitty"),
    ("alacritty", "Alacritty"),
    ("wezterm", "WezTerm"),
]


def detect_available_apps() -> list[tuple[str, str]]:
    """Return (value, label) pairs for terminal apps found on this system."""
    candidates = _MACOS_CANDIDATES if sys.platform == "darwin" else _LINUX_CANDIDATES
    available: list[tuple[str, str]] = []
    for value, label in candidates:
        if sys.platform == "darwin":
            # On macOS, check if the .app bundle exists
            app_path = Path(f"/Applications/{label}.app")
            if app_path.exists() or shutil.which(value):
                available.append((value, label))
        elif shutil.which(value):
            available.append((value, label))
    return available


_MACOS_APP_NAMES: dict[str, str] = {
    "iterm2": "iTerm",
    "iterm": "iTerm",
    "terminal": "Terminal",
    "kitty": "kitty",
    "alacritty": "Alacritty",
    "wezterm": "WezTerm",
}


def _launch_macos_window(cmd_str: str, *, cwd: str = "", app: str = "") -> LaunchResult:
    cd_prefix = f"cd {cwd} && " if cwd else ""
    app_name = _MACOS_APP_NAMES.get(app.lower(), "Terminal")

    if app_name == "iTerm":
        script = f"""
        tell application "iTerm"
            create window with default profile
            tell current session of current window
                write text "{cd_prefix}{cmd_str}"
            end tell
        end tell
        """
    else:
        script = f'tell application "{app_name}" to do script "{cd_prefix}{cmd_str}"'

    proc = spawn(["osascript", "-e", script], stdout=DEVNULL, stderr=PIPE)
    logger.info("Launched %s window (pid=%d)", app_name, proc.pid)
    return LaunchResult(pid=proc.pid, mode="new-window")


def _launch_linux_window(cmd_str: str, *, cwd: str = "", app: str = "") -> LaunchResult:
    terminal = shutil.which(app) if app else None
    if not terminal:
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

    proc = spawn(args, stdout=DEVNULL, stderr=PIPE, cwd=cwd or None)
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
