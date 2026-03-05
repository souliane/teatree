#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Squash all worktree commits into one and rebase on origin/<default_branch>.

Usage: wt_finalize [commit message]
Run from inside a worktree after work is done and tested.
"""

import subprocess
import sys

import lib.init
import typer

lib.init.init()

from lib.env import resolve_context
from lib.git import default_branch


def wt_finalize(msg: str = "") -> int:
    try:
        ctx = resolve_context()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        base_branch = default_branch(ctx.main_repo)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Check for uncommitted changes
    diff_result = subprocess.run(
        ["git", "diff", "--quiet"],
        capture_output=True,
        cwd=ctx.wt_dir,
    )
    cached_result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True,
        cwd=ctx.wt_dir,
    )
    if diff_result.returncode != 0 or cached_result.returncode != 0:
        print(
            "Error: worktree has uncommitted changes. Commit or stash them first.",
            file=sys.stderr,
        )
        return 1

    # Fetch latest
    print("Fetching origin...")
    subprocess.run(["git", "-C", ctx.wt_dir, "fetch", "origin"], check=False)

    # Pull main repo
    print(f"Pulling main repo ({base_branch})...")
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=ctx.main_repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            "  Warning: pull failed in main repo (dirty tree or diverged?)",
            file=sys.stderr,
        )

    # Find fork point
    result = subprocess.run(
        ["git", "merge-base", f"origin/{base_branch}", "HEAD"],
        capture_output=True,
        text=True,
        cwd=ctx.wt_dir,
        check=True,
    )
    fork_point = result.stdout.strip()

    result = subprocess.run(
        ["git", "rev-list", "--count", f"{fork_point}..HEAD"],
        capture_output=True,
        text=True,
        cwd=ctx.wt_dir,
        check=True,
    )
    commit_count = int(result.stdout.strip())

    if commit_count == 0:
        print(f"No commits to squash (branch is at origin/{base_branch})")
        return 0

    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        cwd=ctx.wt_dir,
        check=True,
    )
    current_branch = result.stdout.strip()

    print(f"Branch:  {current_branch}")
    print(f"Commits: {commit_count} since origin/{base_branch}")
    print()
    subprocess.run(
        ["git", "log", "--oneline", f"{fork_point}..HEAD"],
        cwd=ctx.wt_dir,
        check=False,
    )
    print()

    # Get default message from last commit if not provided
    if not msg:
        result = subprocess.run(
            ["git", "log", "--format=%s", f"{fork_point}..HEAD"],
            capture_output=True,
            text=True,
            cwd=ctx.wt_dir,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        msg = lines[0] if lines else "squashed commits"

    print(f"Squashing into: {msg}")
    subprocess.run(["git", "reset", "--soft", fork_point], cwd=ctx.wt_dir, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=ctx.wt_dir, check=True)

    # Rebase on latest default branch
    print(f"Rebasing on origin/{base_branch}...")
    result = subprocess.run(
        ["git", "rebase", f"origin/{base_branch}"],
        cwd=ctx.wt_dir,
        check=False,
    )
    if result.returncode != 0:
        print()
        print("Rebase has conflicts. Resolve them, then run:")
        print("  git rebase --continue")
        return 1

    print()
    print(
        f"Done. Branch '{current_branch}' is now 1 commit on top of origin/{base_branch}.",
    )
    print("Review with: git log --oneline -5")
    return 0


app = typer.Typer(add_completion=False)


@app.command()
def main(
    msg: list[str] | None = typer.Argument(None, help="Commit message words (joined with spaces)"),
) -> None:
    sys.exit(wt_finalize(" ".join(msg) if msg else ""))


if __name__ == "__main__":
    app()
