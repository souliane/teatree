"""XDG-compliant data paths — leaf module with no teatree dependencies.

Teatree worktree checkouts run unmerged code, including unmerged control-DB
migrations. Applying those to the real canonical DB corrupts the migration
history the installed ``t3`` and the live loop depend on. This module makes
that outcome impossible regardless of entry point: worktree code is
auto-isolated onto a per-worktree DB copy, and an explicit attempt to point
worktree code at the true canonical DB is a hard error.
"""

import hashlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

_TRUE_CANONICAL_DATA_DIR = Path.home() / ".local" / "share" / "teatree"
_TRUE_CANONICAL_DB = _TRUE_CANONICAL_DATA_DIR / "db.sqlite3"


class CanonicalDBFromWorktreeError(RuntimeError):
    """Raised when worktree code is pointed at the real canonical control DB."""

    def __init__(self, repo_root: Path) -> None:
        message = (
            f"Refusing to use the canonical control DB from a worktree checkout "
            f"({repo_root}). Unset XDG_DATA_HOME so it auto-isolates, or run via "
            f"`t3` (which isolates automatically). If a `t3` command is broken, "
            f"fix it and retry — do not work around it with manual commands."
        )
        super().__init__(message)


def running_from_worktree(repo_root: Path) -> bool:
    """A git worktree has a ``.git`` *file*; a primary clone has a ``.git`` *dir*."""
    return (repo_root / ".git").is_file()


def _code_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_data_dir(*, env: dict[str, str], home: Path, repo_root: Path) -> Path:
    """Resolve the teatree data dir.

    Primary clone: ``$XDG_DATA_HOME/teatree`` (or ``~/.local/share/teatree``) —
    unchanged. Worktree code: auto-isolated onto a deterministic per-worktree
    path unless the caller explicitly chose a sandbox via ``XDG_DATA_HOME``.
    Worktree code resolving to the true canonical dir is refused — use ``t3``
    (which isolates automatically) or fix the broken ``t3`` command and retry;
    never work around it.
    """
    explicit = env.get("XDG_DATA_HOME")
    base = Path(explicit) if explicit else home / ".local" / "share"
    data_dir = base / "teatree"
    if not running_from_worktree(repo_root):
        return data_dir
    true_canonical = home / ".local" / "share" / "teatree"
    if explicit and data_dir.resolve() == true_canonical.resolve():
        raise CanonicalDBFromWorktreeError(repo_root)
    if explicit:
        return data_dir
    slug = hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]
    return home / ".local" / "share" / "teatree" / "_worktrees" / slug


def seed_isolated_db(data_dir: Path) -> None:
    """Copy the true canonical DB into an auto-isolated worktree dir on first use.

    Branch migrations then run against a snapshot of merged state, never the
    original. No-op for the canonical dir itself and when the copy already
    exists or there is nothing to copy.
    """
    if data_dir.resolve() == _TRUE_CANONICAL_DATA_DIR.resolve():
        return
    target = data_dir / "db.sqlite3"
    if target.exists() or not _TRUE_CANONICAL_DB.exists():
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_TRUE_CANONICAL_DB, target)


DATA_DIR = resolve_data_dir(env=dict(os.environ), home=Path.home(), repo_root=_code_repo_root())
CANONICAL_DB = DATA_DIR / "db.sqlite3"


def get_data_dir(namespace: str) -> Path:
    data_dir = DATA_DIR / namespace
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def find_stale_dbs(data_dir: Path, *, canonical: Path) -> Iterator[Path]:
    """Yield ``db.sqlite3`` files inside ``data_dir`` that aren't ``canonical``.

    Walks recursively under ``data_dir`` so any legacy namespaced layout
    (``data_dir/<name>/db.sqlite3``) surfaces. The canonical path is skipped.
    Used by both the settings warning and the ``t3 doctor`` check.
    """
    if not data_dir.is_dir():
        return
    canonical = canonical.resolve()
    for candidate in data_dir.glob("**/db.sqlite3"):
        if candidate.resolve() == canonical:
            continue
        yield candidate
