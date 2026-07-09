"""Teatree main-clone resolution and validation for ``t3 setup``."""

import os
import re
from pathlib import Path

import typer

from teatree.cli.doctor import DoctorService


def find_main_clone() -> Path | None:
    """Find the teatree main clone, resolving worktrees to their main clone.

    The ``T3_REPO`` env var (exported in the user's shell profile)
    wins over cwd heuristics so that ``t3 setup`` run from a worktree still
    targets the configured main clone.  When unset, fall back to
    ``DoctorService.find_teatree_repo`` (cwd → ``find_project_root``); if
    that returns a worktree, follow its ``.git`` file back to the main clone
    so setup targets (``uv tool install --editable`` and Claude plugin
    symlink) land on a stable path.
    """
    env_path = os.environ.get("T3_REPO", "")
    if env_path:
        candidate = Path(env_path).expanduser()
        if (candidate / "pyproject.toml").is_file() and (candidate / ".git").is_dir():
            return candidate

    repo = DoctorService.find_teatree_repo()
    if not repo:
        return None
    git = repo / ".git"
    if git.is_dir():
        return repo
    if git.is_file():
        match = re.match(r"^gitdir:\s*(.+)$", git.read_text().strip())
        if not match:
            return None
        # `.git` points to `<main-clone>/.git/worktrees/<name>`; step back up to main clone.
        main_clone_git = Path(match.group(1)).parent.parent
        if main_clone_git.name == ".git" and main_clone_git.is_dir():
            return main_clone_git.parent
    return None


def validate_repo(repo: Path | None) -> Path:
    """Validate the teatree repo is a main clone with apm.yml. Raises typer.Exit on failure."""
    if not repo:
        typer.echo("ERROR Teatree main clone not found.")
        typer.echo("      Set T3_REPO env var or install teatree in editable mode.")
        typer.echo("      Consumers without a local clone: use `apm install -g souliane/teatree`.")
        raise typer.Exit(code=1)

    if not (repo / "apm.yml").is_file():
        typer.echo(f"ERROR apm.yml not found at {repo}")
        raise typer.Exit(code=1)

    return repo
