"""Cancel running/pending pipelines for a branch before pushing.

Used by: t3-ship (before git push).
"""

import sys
from pathlib import Path

import typer

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.gitlab import cancel_pipelines, current_branch, resolve_project_from_remote


def main(
    repo_dir: str = typer.Argument(".", help="Path to git repo (default: current dir)"),
) -> None:
    """Cancel running/pending pipelines for the current branch."""
    branch = current_branch(repo_dir)
    if not branch:
        print("ERROR: Not in a git repo or no branch checked out", file=sys.stderr)
        raise SystemExit(1)

    proj = resolve_project_from_remote(repo_dir)
    if not proj:
        print("ERROR: Could not resolve GitLab project from remote URL", file=sys.stderr)
        raise SystemExit(1)

    cancelled = cancel_pipelines(proj.project_id, branch)
    if cancelled:
        for pid in cancelled:
            print(f"  Cancelled pipeline #{pid}")
        print(f"Cancelled {len(cancelled)} pipeline(s) for {branch}")
    else:
        print(f"No running/pending pipelines for {branch}")
