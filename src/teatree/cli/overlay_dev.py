"""Dev-mode overlay install/uninstall for dogfooding teatree branches."""

import tomllib
from pathlib import Path

import typer

overlay_dev_app = typer.Typer(no_args_is_help=True, help="Dev-mode overlay install/uninstall.")


class OverlayDevError(RuntimeError):
    """Raised when an overlay dev operation can't proceed."""


def _resolve_teatree_worktree(cwd: Path) -> Path:
    for candidate in [cwd, *cwd.parents]:
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        if data.get("project", {}).get("name") != "teatree":
            msg = f"{candidate} is not a teatree worktree"
            raise OverlayDevError(msg)
        git_marker = candidate / ".git"
        if git_marker.is_dir():
            msg = f"{candidate} is the main clone, not a worktree — refusing to install overlays"
            raise OverlayDevError(msg)
        if not git_marker.is_file():
            msg = f"{candidate} has no .git marker"
            raise OverlayDevError(msg)
        return candidate
    msg = f"No teatree worktree found walking up from {cwd}"
    raise OverlayDevError(msg)
