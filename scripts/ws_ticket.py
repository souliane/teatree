#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Create ticket-specific workspace with git worktrees.

Usage: ws_ticket <ticket-number> <description> <repo1> [repo2 ...]
Creates: $T3_WORKSPACE_DIR/$T3_BRANCH_PREFIX-<first-repo>-<ticket>-<description>/<repo>/
"""

import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import lib.init
import typer

lib.init.init()

from lib.env import branch_prefix, workspace_dir


def _link_python_version(repo_path: Path, wt_path: Path) -> None:
    pv = repo_path / ".python-version"
    pv_dest = wt_path / ".python-version"
    if not pv.is_file() or pv_dest.exists():
        return
    with suppress(OSError):
        pv_dest.symlink_to(pv)


def _rollback_created_worktrees(
    created_wts: list[str],
    ws: str,
    branch_name: str,
    ticket_dir: Path,
) -> None:
    for created in created_wts:
        created_repo = Path(created).name
        created_main = str(Path(ws) / created_repo)
        subprocess.run(
            ["git", "worktree", "remove", "--force", created],
            cwd=created_main,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-d", branch_name],
            cwd=created_main,
            capture_output=True,
        )
    with suppress(OSError):
        ticket_dir.rmdir()


def _create_repo_worktree(
    ws: str,
    repo: str,
    ticket_dir: Path,
    branch_name: str,
) -> tuple[str, bool]:
    repo_path = Path(ws) / repo
    if not (repo_path / ".git").is_dir():
        print(f"Skipping: {repo_path} is not a git repository", file=sys.stderr)
        return "", True

    wt_path = ticket_dir / repo
    if wt_path.exists():
        print(f"Skipping: {wt_path} already exists", file=sys.stderr)
        return "", True

    print(f"Updating {repo}...")
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  Warning: git pull failed for {repo} (dirty tree or diverged?), continuing with current state",
            file=sys.stderr,
        )

    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(wt_path)],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"Error: failed to create worktree for {repo} — cleaning up",
            file=sys.stderr,
        )
        print(result.stderr, file=sys.stderr)
        return "", False

    _link_python_version(repo_path, wt_path)
    return str(wt_path), True


def ws_ticket(ticket_number: str, description: str, repos: list[str]) -> int:
    ws = workspace_dir()
    prefix = branch_prefix()
    first_repo = repos[0]
    branch_name = f"{prefix}-{first_repo}-{ticket_number}-{description}"
    ticket_dir = Path(ws) / branch_name
    if not Path(ws).is_dir():
        print(f"Base workspace not found: {ws}", file=sys.stderr)
        return 1

    ticket_dir.mkdir(parents=True, exist_ok=True)
    created_wts: list[str] = []

    for repo in repos:
        created, ok = _create_repo_worktree(ws, repo, ticket_dir, branch_name)
        if not ok:
            _rollback_created_worktrees(created_wts, ws, branch_name, ticket_dir)
            return 1
        if created:
            created_wts.append(created)

    if not created_wts:
        print(
            "Error: no worktrees were created (all repositories were skipped).",
            file=sys.stderr,
        )
        return 1

    print(f"Worktree(s) created in: {ticket_dir}")
    print(f"  Branch: {branch_name}")
    print()
    print("Next steps:")
    print(f"  cd {ticket_dir / first_repo}")
    print("  t3_setup [variant]    # set up environment, DB, etc.")
    return 0


app = typer.Typer(add_completion=False)


@app.command()
def main(
    ticket_number: str = typer.Argument(..., help="Ticket number (e.g. 1234)"),
    description: str = typer.Argument(..., help="Short description for the branch name"),
    repos: list[str] = typer.Argument(..., help="Repository names to create worktrees for"),
) -> None:
    sys.exit(ws_ticket(ticket_number, description, repos))


if __name__ == "__main__":
    app()
