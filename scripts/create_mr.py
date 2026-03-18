"""Create a merge request from the current branch.

Parses commit message, reads ticket URL from .env.worktree, validates via
the wt_validate_mr extension point, and creates the MR via GitLab API.

Used by: t3-ship.
"""

import os
import subprocess
import sys
from pathlib import Path

import typer

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.env import detect_ticket_dir, read_env_key
from lib.git import default_branch
from lib.gitlab import (
    create_mr,
    current_branch,
    current_user,
    resolve_project_from_remote,
)


def _last_commit_message(repo_dir: str = ".") -> str:
    result = subprocess.run(
        ["git", "-C", repo_dir, "log", "-1", "--format=%B"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _build_mr_title(commit_first_line: str, ticket_url: str) -> str:
    """Build MR title from commit message first line + ticket URL."""
    # If already has a ticket URL, use as-is
    if "https://gitlab.com/" in commit_first_line:
        return commit_first_line
    # Append ticket URL
    if ticket_url:
        return f"{commit_first_line} ({ticket_url})"
    return commit_first_line


def _build_mr_description(title: str, commit_body: str) -> str:
    """Build MR description: first line = full title, then body."""
    parts = [title, ""]
    if commit_body.strip():
        parts.append(commit_body.strip())
    else:
        parts.extend(["## Summary", "", "- "])
    return "\n".join(parts)


def _split_commit_message(msg: str) -> tuple[str, str]:
    """Split commit message into (first_line, body)."""
    first_line, _, body = msg.partition("\n")
    return first_line, body.strip()


def _auto_labels() -> list[str] | None:
    """Read T3_MR_AUTO_LABELS env var and return parsed labels or None."""
    env = os.environ.get("T3_MR_AUTO_LABELS", "")
    if not env:
        return None
    labels = [lbl.strip() for lbl in env.split(",") if lbl.strip()]
    return labels or None


def _try_validate(title: str, description: str) -> bool:
    """Try to validate MR via project's validate_mr module. Returns True if valid."""
    try:
        from lib.validate_mr import validate_mr  # type: ignore[import-not-found]

        result = validate_mr(title, description)
        if not result.ok:
            for err in result.errors:
                print(f"  VALIDATION ERROR: {err}", file=sys.stderr)
            return False
        for warn in result.warnings:
            print(f"  WARNING: {warn}", file=sys.stderr)
    except ImportError:
        pass  # No project validator available — skip
    return True


def main(
    repo_dir: str = typer.Argument(".", help="Path to git repo"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Print what would be created"),
    skip_validation: bool = typer.Option(False, "--skip-validation", help="Skip MR validation"),
) -> None:
    """Create an MR from the current branch."""
    branch = current_branch(repo_dir)
    if not branch:
        print("ERROR: Not in a git repo", file=sys.stderr)
        raise SystemExit(1)

    proj = resolve_project_from_remote(repo_dir)
    if not proj:
        print("ERROR: Could not resolve GitLab project", file=sys.stderr)
        raise SystemExit(1)

    target = default_branch(repo_dir)
    username = current_user()

    # Get commit message — split into first line and body
    first_line, body = _split_commit_message(_last_commit_message(repo_dir))

    # Get ticket URL from .env.worktree
    ticket_url = ""
    ticket_dir = detect_ticket_dir()
    if ticket_dir:
        ticket_url = read_env_key(str(Path(ticket_dir) / ".env.worktree"), "TICKET_URL")

    title = _build_mr_title(first_line, ticket_url)
    description = _build_mr_description(title, body)

    # Validate
    if not skip_validation and not _try_validate(title, description):
        print("MR validation failed. Fix the issues or use --skip-validation.", file=sys.stderr)
        raise SystemExit(1)

    auto_labels = _auto_labels()

    if dry_run:
        print(f"Project: {proj.path_with_namespace} (ID: {proj.project_id})")
        print(f"Branch:  {branch} → {target}")
        print(f"Title:   {title}")
        print(f"Assign:  {username}")
        if auto_labels:
            print(f"Labels:  {', '.join(auto_labels)}")
        print(f"Description:\n{description}")
        return

    result = create_mr(
        proj.project_id,
        branch,
        target,
        title,
        description,
        assignee_username=username,
        labels=auto_labels,
        squash=True,
    )

    if not result:
        print("ERROR: Failed to create MR", file=sys.stderr)
        raise SystemExit(1)

    mr_url = result.get("web_url", "")
    mr_iid = result.get("iid", "?")
    print(f"Created !{mr_iid}: {mr_url}")
