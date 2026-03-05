#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
"""Prune merged/gone worktrees, branches, Docker services, and databases.

Ticket-atomic: ALL worktrees in a ticket directory must be merged/gone and
clean before ANY are removed.  Also cleans orphaned branches (merged but
no worktree) and tears down Docker resources + databases.

Always scans all repos in $T3_WORKSPACE_DIR for a consistent ticket-level view.
Safe to call multiple times (idempotent).
"""

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import lib.init

lib.init.init()

from lib.env import workspace_dir
from lib.git import default_branch

# Branches that must never be removed, even if they appear "merged into themselves".
_PROTECTED_BRANCHES = frozenset({"main", "master", "development"})

# Untracked paths that are safe to ignore during dirty checks.
# Checked against the first path component of each untracked file.
_IGNORED_UNTRACKED = frozenset(
    {*os.environ.get("T3_SHARED_DIRS", ".data").split(","), "uv.lock"},
)


@dataclass
class WorktreeInfo:
    """Info about a single worktree inside a ticket directory."""

    repo: str  # main repo path (e.g. ~/workspace/my-backend)
    wt_path: str  # worktree path (e.g. ~/workspace/<ticket>/my-backend)
    wt_branch: str  # branch name
    is_removable: bool  # True if merged into default or upstream is [gone]
    dirty_reason: str  # empty if clean


@dataclass
class TicketEnv:
    """Values read from a ticket's .env.worktree."""

    db_name: str = ""
    compose_project_name: str = ""


# ---------------------------------------------------------------------------
# Repo / branch discovery
# ---------------------------------------------------------------------------


def _discover_repos(ws: str) -> list[str]:
    """Discover all git repos in the workspace directory."""
    return sorted(entry.path for entry in os.scandir(ws) if entry.is_dir() and (Path(entry.path) / ".git").is_dir())


def _fetch_and_report(repo: str) -> None:
    result = subprocess.run(
        ["git", "fetch", "--all", "--prune"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    for line in result.stderr.splitlines():
        if "From " in line or "[deleted]" in line:
            print(line)


def _parse_worktrees(porcelain_output: str) -> list[tuple[str, str]]:
    worktrees: list[tuple[str, str]] = []
    current_path = ""
    current_branch = ""
    for line in porcelain_output.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
            continue
        if line.startswith("branch "):
            current_branch = line[len("branch refs/heads/") :]
            continue
        if line or not current_path or not current_branch:
            continue
        worktrees.append((current_path, current_branch))
        current_path = ""
        current_branch = ""
    if current_path and current_branch:
        worktrees.append((current_path, current_branch))
    return worktrees


def _is_branch_merged(repo: str, wt_branch: str, default: str) -> bool:
    result = subprocess.run(
        ["git", "branch", "--merged", f"origin/{default}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return any(line.strip() == wt_branch for line in result.stdout.splitlines())


def _is_branch_gone(repo: str, wt_branch: str) -> bool:
    result = subprocess.run(
        ["git", "branch", "--format", "%(refname:short) %(upstream:track)"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return any(line.strip() == f"{wt_branch} [gone]" for line in result.stdout.splitlines())


def _dirty_reason(wt_path: str) -> str:
    if (
        subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=wt_path,
            capture_output=True,
        ).returncode
        != 0
    ):
        return "uncommitted changes"
    if (
        subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=wt_path,
            capture_output=True,
        ).returncode
        != 0
    ):
        return "uncommitted changes"
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=wt_path,
        capture_output=True,
        text=True,
    )
    significant = [
        f for f in untracked.stdout.strip().splitlines() if f and f.split("/", 1)[0] not in _IGNORED_UNTRACKED
    ]
    return "untracked files" if significant else ""


def _all_local_branches(repo: str, default: str) -> list[str]:
    """List all local branches except default."""
    result = subprocess.run(
        ["git", "branch", "--format", "%(refname:short)"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return [b.strip() for b in result.stdout.splitlines() if b.strip() and b.strip() != default]


def _ticket_dir_for_worktree(ws: str, wt_path: str) -> str | None:
    """Extract ticket dir from worktree path, or None if not under workspace."""
    parent = str(Path(wt_path).parent)
    if not parent.startswith(ws + "/"):
        return None
    if (Path(parent) / ".git").is_dir():
        return None  # parent is a main repo, not a ticket dir
    return parent


def _extract_ticket_number(ticket_dir_name: str) -> str:
    match = re.search(r"\d+", ticket_dir_name)
    return match.group() if match else ""


# ---------------------------------------------------------------------------
# Inventory — build ticket-level map + orphaned branches
# ---------------------------------------------------------------------------


def _inventory(
    repos: list[str],
    ws: str,
) -> tuple[dict[str, list[WorktreeInfo]], list[tuple[str, str]]]:
    """Scan all repos, group worktrees by ticket dir, find orphaned branches.

    Returns:
        ticket_map: {ticket_dir: [WorktreeInfo, ...]}
        orphan_branches: [(repo, branch), ...] — merged branches without worktrees

    """
    ticket_map: dict[str, list[WorktreeInfo]] = {}
    orphan_branches: list[tuple[str, str]] = []

    for repo in repos:
        repo_name = Path(repo).name
        print(f"=== {repo_name} ===")
        _fetch_and_report(repo)

        try:
            default = default_branch(repo)
        except RuntimeError:
            print("  Could not detect default branch, skipping")
            continue

        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        worktree_branches: set[str] = {default}

        for wt_path, wt_branch in _parse_worktrees(wt_result.stdout):
            worktree_branches.add(wt_branch)
            if wt_path == toplevel:
                continue
            if wt_branch in _PROTECTED_BRANCHES:
                continue

            ticket_dir = _ticket_dir_for_worktree(ws, wt_path)
            if not ticket_dir:
                continue

            is_removable = _is_branch_merged(
                repo,
                wt_branch,
                default,
            ) or _is_branch_gone(repo, wt_branch)
            dirty = _dirty_reason(wt_path) if is_removable else ""

            ticket_map.setdefault(ticket_dir, []).append(
                WorktreeInfo(repo, wt_path, wt_branch, is_removable, dirty),
            )

        # Find orphaned branches (merged/gone, no worktree)
        for branch in _all_local_branches(repo, default):
            if branch in worktree_branches or branch in _PROTECTED_BRANCHES:
                continue
            if _is_branch_merged(repo, branch, default) or _is_branch_gone(
                repo,
                branch,
            ):
                orphan_branches.append((repo, branch))

    return ticket_map, orphan_branches


# ---------------------------------------------------------------------------
# Docker / DB cleanup
# ---------------------------------------------------------------------------


def _read_ticket_env(ticket_dir: str) -> TicketEnv:
    """Read cleanup-relevant values from a ticket's .env.worktree."""
    env_path = Path(ticket_dir) / ".env.worktree"
    result = TicketEnv()
    if not env_path.is_file():
        return result
    try:
        with env_path.open() as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("WT_DB_NAME="):
                    result.db_name = line.split("=", 1)[1]
                elif line.startswith("COMPOSE_PROJECT_NAME="):
                    result.compose_project_name = line.split("=", 1)[1]
    except OSError:  # pragma: no cover
        pass
    return result


def _has_compose_file(wt_path: str) -> bool:
    return (Path(wt_path) / "docker-compose.yml").is_file() or (Path(wt_path) / "compose.yml").is_file()


def _docker_rm_by_label(project_name: str) -> None:
    """Remove leftover Docker containers, volumes, and networks by project label."""
    label = f"com.docker.compose.project={project_name}"

    # Containers
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label={label}", "-q"],
        capture_output=True,
        text=True,
    )
    ids = result.stdout.split()
    if ids and ids[0]:
        subprocess.run(
            ["docker", "rm", "-f", *ids],
            capture_output=True,
            check=False,
        )

    # Volumes
    result = subprocess.run(
        ["docker", "volume", "ls", "--filter", f"label={label}", "-q"],
        capture_output=True,
        text=True,
    )
    ids = result.stdout.split()
    if ids and ids[0]:
        subprocess.run(
            ["docker", "volume", "rm", "-f", *ids],
            capture_output=True,
            check=False,
        )

    # Networks
    result = subprocess.run(
        ["docker", "network", "ls", "--filter", f"label={label}", "-q"],
        capture_output=True,
        text=True,
    )
    ids = result.stdout.split()
    if ids and ids[0]:
        subprocess.run(
            ["docker", "network", "rm", *ids],
            capture_output=True,
            check=False,
        )


def _try_drop_host_db(db_name: str) -> None:
    """Best-effort DB drop on localhost:5432 (host postgres)."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", db_name):
        return

    pg_env = {
        **os.environ,
        "PGPASSWORD": os.environ.get("POSTGRES_PASSWORD", "local_superpassword"),
    }
    pg_user = os.environ.get("POSTGRES_USER", "local_superuser")

    # Check if DB exists on host postgres
    result = subprocess.run(
        [
            "psql",
            "-h",
            "localhost",
            "-p",
            "5432",
            "-U",
            pg_user,
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'",  # noqa: S608
        ],
        env=pg_env,
        capture_output=True,
        text=True,
    )
    if "1" not in result.stdout:
        return

    print(f"  Dropping database: {db_name}")
    # Terminate active connections
    subprocess.run(
        [
            "psql",
            "-h",
            "localhost",
            "-p",
            "5432",
            "-U",
            pg_user,
            "-d",
            "postgres",
            "-c",
            (
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608
                f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
            ),
        ],
        env=pg_env,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [
            "dropdb",
            "-h",
            "localhost",
            "-p",
            "5432",
            "-U",
            pg_user,
            "--if-exists",
            db_name,
        ],
        env=pg_env,
        capture_output=True,
        check=False,
    )


def _cleanup_docker_and_db(
    ticket_dir: str,
    worktrees: list[WorktreeInfo],
    ticket_number: str,
) -> None:
    """Tear down Docker services and databases for a ticket."""
    resources = _read_ticket_env(ticket_dir)

    # Collect all potential compose project names
    compose_names: set[str] = set()
    if resources.compose_project_name:
        compose_names.add(resources.compose_project_name)
    compose_names.update(f"{Path(wt.wt_path).name}-wt{ticket_number}" for wt in worktrees)

    # 1. docker compose down for each worktree with a compose file
    for wt in worktrees:
        if not _has_compose_file(wt.wt_path):
            continue
        env = {**os.environ}
        if resources.compose_project_name:
            env["COMPOSE_PROJECT_NAME"] = resources.compose_project_name
        print(f"  Stopping Docker services: {Path(wt.wt_path).name}")
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                wt.wt_path,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            env=env,
            capture_output=True,
            check=False,
        )

    # 2. Catch-all: clean leftover containers/volumes/networks by label
    for name in sorted(compose_names):
        _docker_rm_by_label(name)

    # 3. Safety net: drop DB from host postgres if it exists there
    if resources.db_name:
        _try_drop_host_db(resources.db_name)


# ---------------------------------------------------------------------------
# Worktree / branch / ticket-dir removal
# ---------------------------------------------------------------------------


def _remove_worktree(repo: str, wt_path: str, wt_branch: str) -> None:
    print(f"  Removing worktree: {Path(wt_path).name} (branch: {wt_branch})")
    subprocess.run(
        ["git", "worktree", "remove", "--force", wt_path],
        cwd=repo,
        check=False,
    )
    subprocess.run(
        ["git", "branch", "-D", wt_branch],
        cwd=repo,
        capture_output=True,
        check=False,
    )


def _remove_ticket_dir(ticket_dir: str) -> None:
    """Remove .env.worktree, generated files, and the ticket directory itself."""
    td = Path(ticket_dir)
    if not td.is_dir():
        return

    # Remove known generated files
    for name in (".env.worktree", "frontend.log"):
        f = td / name
        if f.is_file() or f.is_symlink():
            f.unlink()

    # Remove .direnv cache
    direnv = td / ".direnv"
    if direnv.is_dir():
        shutil.rmtree(direnv)

    # Try to remove the directory (only succeeds if empty)
    try:
        td.rmdir()
        print(f"  Removed ticket dir: {td.name}")
    except OSError:
        remaining = list(td.iterdir())
        print(
            f"  WARNING: ticket dir not empty ({len(remaining)} items remain): {td.name}",
        )
        for item in remaining[:5]:
            print(f"    - {item.name}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _clean_orphan_branches(orphan_branches: list[tuple[str, str]]) -> None:
    """Delete merged branches that have no associated worktree."""
    if not orphan_branches:
        return
    print()
    print("=== Orphaned branches (merged, no worktree) ===")
    for repo, branch in orphan_branches:
        print(f"  {Path(repo).name}: deleting {branch}")
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo,
            capture_output=True,
            check=False,
        )


def _process_ticket(ticket_dir: str, worktrees: list[WorktreeInfo]) -> None:
    """Evaluate and optionally clean up a single ticket directory."""
    td_name = Path(ticket_dir).name
    ticket_number = _extract_ticket_number(td_name)

    not_merged = [wt for wt in worktrees if not wt.is_removable]
    dirty = [wt for wt in worktrees if wt.is_removable and wt.dirty_reason]

    if not_merged:
        repos_str = ", ".join(Path(wt.wt_path).name for wt in not_merged)
        print(f"  {td_name}: SKIP — not merged: {repos_str}")
        return

    if dirty:
        for wt in dirty:
            print(
                f"  {td_name}: SKIP — {Path(wt.wt_path).name} has {wt.dirty_reason}",
            )
        return

    # All worktrees merged/gone and clean — full cleanup
    repos_str = ", ".join(Path(wt.wt_path).name for wt in worktrees)
    print(f"  {td_name}: all merged ({repos_str}) — cleaning up")

    _cleanup_docker_and_db(ticket_dir, worktrees, ticket_number)

    for wt in worktrees:
        _remove_worktree(wt.repo, wt.wt_path, wt.wt_branch)

    _remove_ticket_dir(ticket_dir)


def git_clean_them_all() -> int:
    ws = workspace_dir()
    repos = _discover_repos(ws)
    if not repos:
        print(f"No git repos found in {ws}", file=sys.stderr)
        return 1

    ticket_map, orphan_branches = _inventory(repos, ws)

    _clean_orphan_branches(orphan_branches)

    if ticket_map:
        print()
        print("=== Ticket directories ===")
        for ticket_dir in sorted(ticket_map):
            _process_ticket(ticket_dir, ticket_map[ticket_dir])

    # Prune stale worktree references
    for repo in repos:
        subprocess.run(["git", "worktree", "prune"], cwd=repo, check=False)

    return 0


def main() -> None:
    sys.exit(git_clean_them_all())


if __name__ == "__main__":
    main()
