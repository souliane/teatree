"""Worktree resolution from the user's original CWD.

Resolution order:

1. Walk up from CWD looking for ``.env.worktree`` → parse ``TICKET_DIR`` →
    match against ``Worktree.extra["worktree_path"]`` in the DB
2. Match CWD directly against ``Worktree.extra["worktree_path"]``
3. Detect git worktree from filesystem and auto-register in DB

``T3_ORIG_CWD`` env var (set by the CLI) preserves the user's shell CWD
across the ``uv --directory`` subprocess chain.
"""

import logging
import os
import subprocess  # noqa: S404
from pathlib import Path

from teatree.core.models import Ticket, Worktree

logger = logging.getLogger(__name__)


class WorktreeNotFoundError(RuntimeError):
    """Raised when no worktree can be resolved from the current context."""


def _get_user_cwd() -> str:
    """Return the user's original CWD, surviving ``uv --directory`` and subprocess chains."""
    return os.environ.get("T3_ORIG_CWD", os.environ.get("PWD", str(Path.cwd())))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE env file (no shell expansion)."""
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _find_env_worktree(cwd: str) -> Path | None:
    """Walk up from *cwd* looking for ``.env.worktree``."""
    cwd_path = Path(cwd)
    for parent in [cwd_path, *cwd_path.parents]:
        candidate = parent / ".env.worktree"
        if candidate.is_file():
            return candidate
    return None


def _match_worktree_by_path(path: str) -> Worktree | None:
    """Find a Worktree whose ``extra["worktree_path"]`` matches or contains *path*.

    First tries an exact DB-level JSON lookup, then falls back to a prefix
    match for when the user is in a subdirectory of the worktree.
    """
    # Fast path: exact match via DB-level JSON lookup
    exact = Worktree.objects.filter(extra__worktree_path=path).first()
    if exact is not None:
        return exact

    # Walk up from path to find a parent that matches a stored worktree_path.
    # This handles being inside a subdirectory of a worktree.
    path_obj = Path(path)
    for parent in path_obj.parents:
        parent_str = str(parent)
        match = Worktree.objects.filter(extra__worktree_path=parent_str).first()
        if match is not None:
            return match
        # Stop at filesystem root or home directory to avoid excessive queries
        if parent_str == str(Path.home()) or parent == parent.parent:
            break

    return None


def _auto_register_from_git(cwd: str) -> Worktree | None:
    """Detect a git worktree from the filesystem and auto-register it in the DB."""
    cwd_path = Path(cwd)
    git_file = cwd_path / ".git"
    if not git_file.is_file():
        return None  # Not a git worktree (worktrees have .git as a file, not dir)

    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],  # noqa: S607
            cwd=cwd,
            text=True,
            timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if not branch:
        return None

    repo_name = cwd_path.name
    ticket, _created = Ticket.objects.get_or_create(
        issue_url=f"auto:{branch}",
        defaults={"variant": "", "repos": [repo_name]},
    )
    wt, _wt_created = Worktree.objects.get_or_create(
        ticket=ticket,
        repo_path=repo_name,
        defaults={
            "branch": branch,
            "extra": {"worktree_path": cwd},
        },
    )
    return wt


def _is_main_clone(path: str) -> bool:
    """Return True if *path* is a main git clone (not a worktree).

    Git worktrees have ``.git`` as a file pointing to the main repo's
    ``.git/worktrees/<name>`` directory. Main clones have ``.git`` as a
    directory.
    """
    git_marker = Path(path) / ".git"
    return git_marker.is_dir()


def resolve_worktree(path: str = "") -> Worktree:
    """Resolve a worktree from *path* or the user's CWD.

    Raises ``WorktreeNotFoundError`` if no worktree can be found or if
    the resolved path is a main repo clone (not a worktree).
    """
    cwd = str(Path(path).resolve()) if path else _get_user_cwd()

    # 1. Walk up from CWD to find .env.worktree
    envfile = _find_env_worktree(cwd)
    if envfile is not None:
        env = _parse_env_file(envfile)
        ticket_dir = env.get("TICKET_DIR", "")
        if ticket_dir:
            wt = _match_worktree_by_path(ticket_dir)
            if wt is not None:  # pragma: no branch
                return wt

    # 2. Match CWD directly against stored worktree paths
    wt = _match_worktree_by_path(cwd)
    if wt is not None:
        wt_path = (wt.extra or {}).get("worktree_path", "")
        if wt_path and _is_main_clone(wt_path):
            msg = (
                f"Refusing to operate on main clone at {wt_path}.\n"
                "Create a worktree first: t3 <overlay> workspace ticket <issue_url>"
            )
            raise WorktreeNotFoundError(msg)
        return wt

    # 3. Detect git worktree from filesystem and auto-register
    wt = _auto_register_from_git(cwd)
    if wt is not None:
        return wt

    msg = f"Cannot auto-detect worktree from {cwd}.\nMake sure you are running t3 from inside a worktree directory."
    raise WorktreeNotFoundError(msg)
