"""CLI commands for plugin installation and management."""

import shutil
import subprocess  # noqa: S404
from pathlib import Path

import typer

plugin_app = typer.Typer(no_args_is_help=True, help="Plugin installation and management.")

_PLUGIN_NAME = "t3"
_MARKETPLACE_NAME = "souliane"


def _teatree_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _try_apm_install() -> bool:
    apm_path = shutil.which("apm")
    if not apm_path:
        return False
    result = subprocess.run(  # noqa: S603
        [apm_path, "install", "souliane/teatree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _ensure_marketplace(claude_bin: str) -> bool:
    """Register the teatree repo as a local marketplace if not already present."""
    root = _teatree_root()
    result = subprocess.run(  # noqa: S603
        [claude_bin, "plugin", "marketplace", "add", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 or "already" in result.stderr.lower()


def _try_claude_plugin_install(*, scope: str, dev: bool) -> bool:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False
    if dev:
        root = _teatree_root()
        typer.echo(f"Dev mode: use --plugin-dir {root} when launching claude.")
        typer.echo(f"Example: claude --plugin-dir {root}")
        return True

    if not _ensure_marketplace(claude_bin):
        return False

    plugin_id = f"{_PLUGIN_NAME}@{_MARKETPLACE_NAME}"
    cmd = [claude_bin, "plugin", "install", plugin_id, "--scope", scope]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        typer.echo(f"Claude plugin install failed: {result.stderr.strip()}")
        return False
    return True


@plugin_app.command()
def install(
    *,
    scope: str = typer.Option("user", help="Installation scope: user or project."),
    dev: bool = typer.Option(False, "--dev", help="Development mode: use --plugin-dir."),
) -> None:
    """Install the t3 plugin for Claude Code.

    Tries APM first, then Claude CLI (registers local marketplace + installs).
    """
    if not dev and _try_apm_install():
        typer.echo("Installed via APM.")
        return

    if _try_claude_plugin_install(scope=scope, dev=dev):
        if not dev:
            typer.echo("Installed via Claude CLI.")
        return

    typer.echo("Installation failed. Install manually: claude --plugin-dir /path/to/teatree")
    raise typer.Exit(code=1)
