"""Worktree resolution from PWD, env var, or explicit ID.

Replicates the old ``resolve_context()`` behavior from ``scripts/lib/env.py``.
The resolution order:

1. Explicit ``worktree_id`` argument (if non-zero)
2. ``WT_ID`` env var
3. Walk up from PWD looking for ``.env.worktree`` → parse ``TICKET_DIR`` →
    match against ``Worktree.extra["worktree_path"]`` in the DB
4. Match PWD directly against ``Worktree.extra["worktree_path"]``
"""

import os
from pathlib import Path

from teetree.core.models import Worktree


class WorktreeNotFoundError(RuntimeError):
    """Raised when no worktree can be resolved from the current context."""


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


def _find_env_worktree_from_cwd() -> Path | None:
    """Walk up from PWD looking for ``.env.worktree``."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".env.worktree"
        if candidate.is_file():
            return candidate
    return None


def _match_worktree_by_path(path: str) -> Worktree | None:
    """Find a Worktree whose ``extra["worktree_path"]`` matches or contains ``path``."""
    for wt in Worktree.objects.exclude(extra={}).exclude(extra__isnull=True):
        wt_path = (wt.extra or {}).get("worktree_path", "")
        if wt_path and path.startswith(wt_path):
            return wt
    return None


def resolve_worktree(worktree_id: int = 0) -> Worktree:
    """Resolve a worktree from explicit ID, env var, or PWD.

    Raises ``WorktreeNotFoundError`` if no worktree can be found.
    """
    # 1. Explicit ID
    if worktree_id:
        return Worktree.objects.get(pk=worktree_id)

    # 2. WT_ID env var
    env_id = os.environ.get("WT_ID", "")
    if env_id.isdigit() and int(env_id) > 0:
        return Worktree.objects.get(pk=int(env_id))

    # 3. Walk up from PWD to find .env.worktree
    envfile = _find_env_worktree_from_cwd()
    if envfile is not None:
        env = _parse_env_file(envfile)
        ticket_dir = env.get("TICKET_DIR", "")
        if ticket_dir:
            wt = _match_worktree_by_path(ticket_dir)
            if wt is not None:  # pragma: no branch
                return wt

    # 4. Match PWD directly against stored worktree paths
    cwd = str(Path.cwd())
    wt = _match_worktree_by_path(cwd)
    if wt is not None:
        return wt

    msg = (
        "Cannot auto-detect worktree from current directory.\n"
        "Either:\n"
        "  - cd into a worktree directory\n"
        "  - pass the worktree ID as an argument\n"
        "  - set WT_ID=<id> in your environment"
    )
    raise WorktreeNotFoundError(msg)
