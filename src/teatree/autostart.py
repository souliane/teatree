"""Platform-native daemon management for the teatree dashboard.

No Django imports — works without django.setup().
"""

import logging
import os
import subprocess  # noqa: S404
import sys
from importlib.resources import files
from pathlib import Path

logger = logging.getLogger(__name__)


def _stderr(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stderr.decode(errors="replace") if result.stderr else ""


class UnsupportedPlatformError(RuntimeError):
    """Raised when the current platform has no daemon backend."""


def detect_platform() -> str:
    """Return 'launchd' (macOS) or 'systemd' (Linux)."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "linux":
        return "systemd"
    msg = f"Unsupported platform: {sys.platform}. Only macOS (launchd) and Linux (systemd) are supported."
    raise UnsupportedPlatformError(msg)


# ── Path helpers ──────────────────────────────────────────────────────


def _launchd_plist_path(overlay_name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"com.teatree.{overlay_name}.dashboard.plist"


def _systemd_unit_path(overlay_name: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"teatree-{overlay_name}-dashboard.service"


def _log_dir(overlay_name: str) -> Path:
    from teatree.config import get_data_dir  # noqa: PLC0415

    log_dir = get_data_dir(overlay_name) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


# ── Python discovery ─────────────────────────────────────────────────


def _discover_python() -> str:
    """Find the best Python interpreter.

    Resolution order:
    1. ``$VIRTUAL_ENV/bin/python`` — honours an already-activated venv.
    2. ``sys.executable`` — the interpreter running teatree itself.
    """
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        venv_python = Path(virtual_env) / "bin" / "python"
        if venv_python.is_file():
            return str(venv_python)
    return sys.executable


# ── Context resolution ────────────────────────────────────────────────


def _resolve_context(
    overlay_name: str,
    project_path: Path,
    settings_module: str,
    host: str,
    port: int,
) -> dict[str, str]:
    python = _discover_python()
    asgi_module = settings_module.rsplit(".", 1)[0] + ".asgi:application"
    manage_py = str(project_path / "manage.py")
    logs = _log_dir(overlay_name)

    return {
        "overlay_name": overlay_name,
        "python": python,
        "asgi_module": asgi_module,
        "host": host,
        "port": str(port),
        "project_path": str(project_path),
        "settings_module": settings_module,
        "manage_py": manage_py,
        "stdout_log": str(logs / "dashboard.stdout.log"),
        "stderr_log": str(logs / "dashboard.stderr.log"),
    }


# ── Template rendering ────────────────────────────────────────────────


def _render_template(template_name: str, context: dict[str, str]) -> str:
    template_text = files("teatree.templates").joinpath("autostart").joinpath(template_name).read_text(encoding="utf-8")
    return template_text.format_map(context)


# ── Public API ────────────────────────────────────────────────────────


def enable(
    overlay_name: str,
    project_path: Path,
    settings_module: str,
    host: str,
    port: int,
) -> str:
    """Install and activate the dashboard daemon. Returns a status message."""
    platform = detect_platform()
    context = _resolve_context(overlay_name, project_path, settings_module, host, port)

    if platform == "launchd":
        return _launchd_enable(overlay_name, context)
    return _systemd_enable(overlay_name, context)


def disable(overlay_name: str) -> str:
    """Stop and remove the dashboard daemon. Returns a status message."""
    platform = detect_platform()

    if platform == "launchd":
        return _launchd_disable(overlay_name)
    return _systemd_disable(overlay_name)


def log_paths(overlay_name: str) -> dict[str, Path]:
    """Return paths to stdout and stderr log files."""
    logs = _log_dir(overlay_name)
    return {
        "stdout": logs / "dashboard.stdout.log",
        "stderr": logs / "dashboard.stderr.log",
    }


# ── launchd backend ──────────────────────────────────────────────────


def _launchd_enable(overlay_name: str, context: dict[str, str]) -> str:
    plist_path = _launchd_plist_path(overlay_name)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Unload first if already installed
    if plist_path.is_file():
        result = subprocess.run(  # noqa: S603
            ["launchctl", "unload", str(plist_path)],  # noqa: S607
            check=False,
            capture_output=True,
        )
        if result.returncode:
            logger.warning("launchctl unload failed (rc=%d): %s", result.returncode, _stderr(result))

    content = _render_template("launchd.plist.tmpl", context)
    plist_path.write_text(content, encoding="utf-8")

    result = subprocess.run(  # noqa: S603
        ["launchctl", "load", str(plist_path)],  # noqa: S607
        check=False,
        capture_output=True,
    )
    if result.returncode:
        msg = f"launchctl load failed (rc={result.returncode}): {_stderr(result)}"
        raise RuntimeError(msg)

    return f"Dashboard daemon installed and started. URL: http://{context['host']}:{context['port']}/"


def _launchd_disable(overlay_name: str) -> str:
    plist_path = _launchd_plist_path(overlay_name)

    if not plist_path.is_file():
        return f"Autostart not installed for {overlay_name}."

    result = subprocess.run(  # noqa: S603
        ["launchctl", "unload", str(plist_path)],  # noqa: S607
        check=False,
        capture_output=True,
    )
    if result.returncode:
        logger.warning("launchctl unload failed (rc=%d): %s", result.returncode, _stderr(result))
    plist_path.unlink()

    return f"Dashboard daemon removed for {overlay_name}."


# ── systemd backend ──────────────────────────────────────────────────


def _systemd_enable(overlay_name: str, context: dict[str, str]) -> str:
    unit_name = f"teatree-{overlay_name}-dashboard.service"
    unit_path = _systemd_unit_path(overlay_name)
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    content = _render_template("systemd.service.tmpl", context)
    unit_path.write_text(content, encoding="utf-8")

    result = subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)  # noqa: S607
    if result.returncode:
        logger.warning("systemctl daemon-reload failed (rc=%d): %s", result.returncode, _stderr(result))
    result = subprocess.run(["systemctl", "--user", "enable", "--now", unit_name], check=False, capture_output=True)  # noqa: S603, S607
    if result.returncode:
        msg = f"systemctl enable failed (rc={result.returncode}): {_stderr(result)}"
        raise RuntimeError(msg)

    return f"Dashboard daemon installed and started. URL: http://{context['host']}:{context['port']}/"


def _systemd_disable(overlay_name: str) -> str:
    unit_name = f"teatree-{overlay_name}-dashboard.service"
    unit_path = _systemd_unit_path(overlay_name)

    if not unit_path.is_file():
        return f"Autostart not installed for {overlay_name}."

    result = subprocess.run(["systemctl", "--user", "disable", "--now", unit_name], check=False, capture_output=True)  # noqa: S603, S607
    if result.returncode:
        logger.warning("systemctl disable failed (rc=%d): %s", result.returncode, _stderr(result))
    unit_path.unlink()
    result = subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)  # noqa: S607
    if result.returncode:
        logger.warning("systemctl daemon-reload failed (rc=%d): %s", result.returncode, _stderr(result))

    return f"Dashboard daemon removed for {overlay_name}."
