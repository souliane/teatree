"""Worktree resolution from the user's original CWD.

Resolution order:

1. Walk up from CWD looking for the env cache symlink → parse
    ``TICKET_DIR`` → match against ``Worktree.extra["worktree_path"]``
2. Match CWD directly against ``Worktree.extra["worktree_path"]``
3. Detect git worktree from filesystem and auto-register in DB

``T3_ORIG_CWD`` env var (set by the CLI) preserves the user's shell CWD
across the ``uv --directory`` subprocess chain.
"""

import logging
import os
from pathlib import Path

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_env import CACHE_FILENAME
from teatree.utils import git

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


def _find_env_cache(cwd: str) -> Path | None:
    """Walk up from *cwd* looking for the env cache.

    Only returns a path whose target can actually be read. ``is_file()``
    follows symlinks and returns False for broken ones, so this naturally
    skips worktree symlinks whose target is not present in the current
    filesystem view (e.g. Docker mounts that don't include the ticket dir).
    """
    cwd_path = Path(cwd)
    for parent in [cwd_path, *cwd_path.parents]:
        candidate = parent / CACHE_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _candidate_paths(path: str) -> list[str]:
    """Return de-duplicated list of path variants to try for DB lookups.

    On macOS, ``/var`` is a symlink to ``/private/var``, so a path stored
    as ``/var/folders/…`` won't match ``/private/var/folders/…`` (and vice
    versa).  We try the original, the resolved form, and the ``/private``
    prefix stripped/added variants.
    """
    candidates: list[str] = [path]
    resolved = str(Path(path).resolve())
    if resolved != path:
        candidates.append(resolved)
    # macOS: /private/var ↔ /var, /private/tmp ↔ /tmp, /private/etc ↔ /etc
    if path.startswith("/private/"):
        candidates.append(path.removeprefix("/private"))
    else:
        prefixed = "/private" + path
        if Path(prefixed).exists():
            candidates.append(prefixed)
    return candidates


def _match_worktree_by_path(path: str) -> Worktree | None:
    """Find a Worktree whose ``extra["worktree_path"]`` matches or contains *path*.

    First tries an exact DB-level JSON lookup, then falls back to a prefix
    match for when the user is in a subdirectory of the worktree.
    Tries both the original and symlink-resolved path to handle macOS
    ``/var`` → ``/private/var`` differences.
    """
    # Fast path: exact match via DB-level JSON lookup
    for candidate in _candidate_paths(path):
        exact = Worktree.objects.filter(extra__worktree_path=candidate).first()
        if exact is not None:
            return exact

    # Walk up from path to find a parent that matches a stored worktree_path.
    # This handles being inside a subdirectory of a worktree.
    resolved_path = str(Path(path).resolve())
    path_obj = Path(resolved_path)
    for parent in path_obj.parents:
        for candidate in _candidate_paths(str(parent)):
            match = Worktree.objects.filter(extra__worktree_path=candidate).first()
            if match is not None:
                return match
        # Stop at filesystem root or home directory to avoid excessive queries
        parent_str = str(parent)
        if parent_str == str(Path.home()) or parent == parent.parent:
            break

    return None


def _auto_register_from_git(cwd: str) -> Worktree | None:
    """Detect a git worktree from the filesystem and auto-register it in the DB.

    Reuses an existing Worktree row keyed by branch + repo before falling
    through to creating a new ``auto:<branch>`` ticket. This prevents duplicate
    ticket rows when a real-ticket worktree exists but its
    ``extra["worktree_path"]`` is missing or stale (which would make
    ``_match_worktree_by_path`` miss it).
    """
    cwd_path = Path(cwd).resolve()
    git_file = cwd_path / ".git"
    if not git_file.is_file():
        return None  # Not a git worktree (worktrees have .git as a file, not dir)

    branch = git.current_branch(repo=cwd)
    if not branch:
        return None

    repo_name = cwd_path.name
    existing = Worktree.objects.filter(branch=branch, repo_path=repo_name).first()
    if existing is not None:
        extra = existing.extra or {}
        if extra.get("worktree_path") != str(cwd_path):
            extra["worktree_path"] = str(cwd_path)
            existing.extra = extra
            existing.save(update_fields=["extra"])
        return existing

    ticket, _created = Ticket.objects.get_or_create(
        issue_url=f"auto:{branch}",
        defaults={"variant": "", "repos": [repo_name]},
    )
    wt, _wt_created = Worktree.objects.get_or_create(
        ticket=ticket,
        repo_path=repo_name,
        defaults={
            "branch": branch,
            "extra": {"worktree_path": str(cwd_path)},
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


def _warn_cwd_mismatch(worktree: Worktree, cwd: str) -> None:
    """Log a warning when the resolved worktree path and user's CWD are unrelated.

    Either CWD should be inside the worktree path (running from a
    subdirectory), or the worktree path should be inside CWD (running
    from the ticket directory that contains the worktree).
    """
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    if not wt_path:
        return
    cwd_resolved = str(Path(cwd).resolve())
    wt_resolved = str(Path(wt_path).resolve())
    if not cwd_resolved.startswith(wt_resolved) and not wt_resolved.startswith(cwd_resolved):
        logger.warning(
            "Resolved worktree path %s does not match CWD %s. You may be operating on the wrong worktree.",
            wt_resolved,
            cwd_resolved,
        )


def resolve_worktree(path: str = "") -> Worktree:
    """Resolve a worktree from *path* or the user's CWD.

    Raises ``WorktreeNotFoundError`` if no worktree can be found or if
    the resolved path is a main repo clone (not a worktree).

    Logs a warning when the resolved worktree path doesn't contain the
    user's CWD, which may indicate the wrong worktree was matched.
    """
    cwd = str(Path(path).resolve()) if path else _get_user_cwd()

    # 1. Walk up from CWD to find the env cache (symlink resolves to
    #    the canonical file in .t3-cache/).
    envfile = _find_env_cache(cwd)
    if envfile is not None:
        env = _parse_env_file(envfile)
        ticket_dir = env.get("TICKET_DIR", "")
        if ticket_dir:
            wt = _match_worktree_by_path(ticket_dir)
            if wt is not None:  # pragma: no branch
                _warn_cwd_mismatch(wt, cwd)
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
        _warn_cwd_mismatch(wt, cwd)
        return wt

    # 3. Detect git worktree from filesystem and auto-register
    wt = _auto_register_from_git(cwd)
    if wt is not None:
        return wt

    msg = f"Cannot auto-detect worktree from {cwd}.\nMake sure you are running t3 from inside a worktree directory."
    raise WorktreeNotFoundError(msg)
