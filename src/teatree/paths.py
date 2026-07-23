"""XDG-compliant data paths — leaf module with no teatree dependencies.

Teatree worktree checkouts run unmerged code, including unmerged control-DB
migrations. Applying those to the real canonical DB corrupts the migration
history the installed ``t3`` and the live loop depend on. This module makes
that outcome impossible regardless of entry point.

Worktree code is auto-isolated onto a per-worktree DB under the sibling
``teatree-worktrees`` root, never nested under the canonical data dir, so
``find_stale_dbs``/doctor/settings never mistake it for stale state. That DB
is seeded from a consistent SQLite snapshot, atomically, and only for paths
inside the managed isolation root. An explicit attempt to point worktree code
at the true canonical DB is a hard error.
"""

import fcntl
import hashlib
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

_TRUE_CANONICAL_DATA_DIR = Path.home() / ".local" / "share" / "teatree"
#: The one control DB the installed ``t3`` and the live loop operate on. Every
#: ``t3 <ov> <cmd>`` proxies through the main clone (a ``.git`` *dir*), which
#: resolves here. Worktree-resident ``uv run manage.py`` resolves to an
#: isolated sibling DB instead — the #779 cross-DB mismatch. Public so the
#: lifecycle/ship guard can name it in the refusal message.
TRUE_CANONICAL_DB = _TRUE_CANONICAL_DATA_DIR / "db.sqlite3"

# A repo root that is definitionally NOT a worktree (no ``.git`` file), so
# ``ControlDb.for_repo`` takes its primary branch. Lets ``ControlDb.primary``
# reuse the one seam instead of re-deriving the env precedence.
_PRIMARY_CLONE_SENTINEL = Path("/nonexistent-primary-clone")


class ControlDbResolution(NamedTuple):
    """Which control DB this entry point talks to, and whether it was isolated.

    THE single resolution seam (#3514). Subcommands used to disagree about the
    answer — the Django/ORM path auto-isolates a worktree onto a per-worktree DB
    while the pre-Django cold path always resolves the PRIMARY one — with no shared
    implementation of the env precedence and no signal when the two diverged, so a
    ticket written by one subcommand was invisible to the next. Every path derives
    from here now, and :meth:`ControlDb.divergence_message` turns the remaining,
    deliberate divergence into a stated fact.

    *reason* names why this answer was reached, so a diagnostic can quote it.
    """

    path: Path
    isolated: bool
    reason: str


class ResolvedDataDir(NamedTuple):
    """The resolved data dir plus whether it was auto-isolated for a worktree.

    ``auto_isolated`` is ``True`` only for the worktree-without-explicit-XDG
    case — the single case that may be seeded from the canonical DB.
    """

    path: Path
    auto_isolated: bool


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


def resolve_main_clone(repo_root: Path) -> Path | None:
    """Resolve *repo_root* to its primary clone, following a worktree pointer.

    A primary clone (``.git`` is a *dir*) resolves to itself. A git worktree
    (``.git`` is a *file* holding ``gitdir: <main>/.git/worktrees/<name>``)
    resolves back to the main clone the pointer names. Returns ``None`` when
    ``.git`` is neither, or the pointer cannot be parsed back to a ``.git``
    dir. The single source of truth mirrored by ``cli/setup.py`` and
    ``cli/doctor/plugin_repair.py`` (#1507).
    """
    git = repo_root / ".git"
    if git.is_dir():
        return repo_root
    if git.is_file():
        match = re.match(r"^gitdir:\s*(.+)$", git.read_text().strip())
        if not match:
            return None
        # A relative ``gitdir:`` is resolved against the ``.git`` file's own
        # directory (git's gitfile convention), not the process cwd.
        pointer = Path(match.group(1))
        if not pointer.is_absolute():
            pointer = (repo_root / pointer).resolve()
        # `.git` points at `<main-clone>/.git/worktrees/<name>`; step up to the clone.
        main_git = pointer.parent.parent
        if main_git.name == ".git" and main_git.is_dir():
            return main_git.parent
    return None


def _code_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _worktree_isolation_root(home: Path) -> Path:
    """Sibling of the canonical data dir — never recursively scanned by it."""
    return home / ".local" / "share" / "teatree-worktrees"


def auto_isolated_worktrees_dir() -> Path:
    """Public accessor for the per-worktree auto-isolated env-dir root (#779/#291).

    The single root holding every auto-isolated per-worktree env dir
    (``<slug>/db.sqlite3`` + ``logs/``). Two consumers need it: the cross-DB
    guard refuses when the *live Django connection* points at a ``db.sqlite3``
    under this root (``:memory:`` test DBs are never under it, so the guard is
    inert in tests without a test-only branch), and the clean-all reaper removes
    DB-unreferenced child dirs of it left behind when a checkout is gone.
    """
    return _worktree_isolation_root(Path.home())


def isolated_slug(repo_root: Path) -> str:
    """The deterministic child-dir name an auto-isolated worktree env gets.

    The single source of truth for the per-worktree slug: a 12-char SHA-256
    prefix of the worktree checkout's absolute path. :func:`resolve_data_dir`
    builds the isolated dir from this, and the clean-all reaper hashes each
    live ``Worktree`` row's checkout path through it to learn which child dir
    that row owns — so the resolver and the reaper agree on the mapping.
    """
    return hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]


def resolve_data_dir(*, env: dict[str, str], home: Path, repo_root: Path) -> ResolvedDataDir:
    """Resolve the teatree data dir and whether it was auto-isolated.

    Primary clone: ``$XDG_DATA_HOME/teatree`` (or ``~/.local/share/teatree``) —
    unchanged, ``auto_isolated=False``. Worktree code with no explicit
    ``XDG_DATA_HOME``: a deterministic per-worktree dir under the sibling
    ``teatree-worktrees`` root, ``auto_isolated=True``. Worktree code with an
    explicit sandbox ``XDG_DATA_HOME``: that sandbox, ``auto_isolated=False``
    (the caller chose it deliberately; never seed it). Worktree code resolving
    to the true canonical dir is refused — use ``t3`` (which isolates
    automatically) or fix the broken ``t3`` command and retry.
    """
    explicit = env.get("XDG_DATA_HOME")
    base = Path(explicit) if explicit else home / ".local" / "share"
    data_dir = base / "teatree"
    if not running_from_worktree(repo_root):
        return ResolvedDataDir(data_dir, auto_isolated=False)
    if explicit:
        if data_dir.resolve() == (home / ".local" / "share" / "teatree").resolve():
            raise CanonicalDBFromWorktreeError(repo_root)
        return ResolvedDataDir(data_dir, auto_isolated=False)
    return ResolvedDataDir(_worktree_isolation_root(home) / isolated_slug(repo_root), auto_isolated=True)


@dataclass(frozen=True, slots=True)
class ControlDb:
    """Which control DB an entry point talks to, under one ``env`` + ``home`` (#3514).

    Composes the three answers that were separate module functions each repeating the
    same ``(env, home)`` pair: :meth:`for_repo` (this entry point's DB), :meth:`primary`
    (the DB the installed ``t3`` and the live loop use), and :meth:`divergence_message`
    (what to say when the two differ).

    ``home=None`` defers to the running process's ``Path.home()``, and does so LAZILY —
    only on the branch that actually needs it. An explicit ``T3_CONFIG_DB`` already
    fixes the answer, so resolving it must not touch the home tree at all: eagerly
    computing the default made every cold read a home-tree read, which is exactly the
    coupling ``tests/test_no_agent_memory_dependency.py`` forbids.
    """

    env: Mapping[str, str]
    home: Path | None = None

    def for_repo(self, repo_root: Path) -> ControlDbResolution:
        """THE control-DB answer for code resident in *repo_root*.

        First match wins: an explicit ``T3_CONFIG_DB`` (which collapses every path onto
        one DB, the escape hatch for a subcommand that must join the primary), then
        :func:`resolve_data_dir`'s own precedence (``XDG_DATA_HOME``, else the
        auto-isolated per-worktree dir for worktree code, else the canonical dir). Pure
        for an explicit ``home``: it then reads only its own state, so a caller can
        resolve any entry point's answer — including one it is not itself running as —
        which is what makes the divergence describable.
        """
        override = self.env.get("T3_CONFIG_DB")
        if override:
            return ControlDbResolution(Path(override), isolated=False, reason="T3_CONFIG_DB is set explicitly")
        resolved = resolve_data_dir(
            env=dict(self.env),
            home=self.home if self.home is not None else Path.home(),
            repo_root=repo_root,
        )
        reason = (
            "worktree code with no explicit XDG_DATA_HOME is auto-isolated onto its own DB"
            if resolved.auto_isolated
            else "the primary data dir"
        )
        return ControlDbResolution(resolved.path / "db.sqlite3", isolated=resolved.auto_isolated, reason=reason)

    def primary(self) -> Path:
        """The PRIMARY control DB — the same answer a main clone resolves to.

        The worktree-isolation branch is deliberately not taken: the pre-Django cold
        readers must reach the DB the installed ``t3`` and the live loop operate on,
        even when the code they are embedded in lives in a worktree. Derived from
        :meth:`for_repo` against a synthetic primary-clone root so the env precedence
        has ONE implementation, never a second copy that can drift.
        """
        return self.for_repo(_PRIMARY_CLONE_SENTINEL).path

    def divergence_message(self, repo_root: Path) -> str | None:
        """The message naming both DBs when *repo_root*'s answer is not the primary.

        ``None`` when they agree — the ordinary case, and nothing to say. Otherwise the
        isolation is real and intended (worktree code must never migrate the canonical
        DB), so the message states both paths and the remedy rather than pretending it
        away: a stranded ticket is what happens when this stays unsaid.
        """
        mine = self.for_repo(repo_root)
        primary = self.primary()
        if mine.path == primary:
            return None
        return (
            f"This entry point resolves the control DB at {mine.path} ({mine.reason}), while the "
            f"installed `t3` and the live loop use {primary}. A ticket written here is NOT visible "
            f"there. Run `t3 <overlay> worktree provision` to provision this worktree, or set "
            f"T3_CONFIG_DB to join a specific DB deliberately."
        )


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """Consistent point-in-time copy even if a live writer holds ``src``.

    ``?immutable=1`` (not ``?mode=ro``) opens the source: a WAL-mode DB whose
    ``-shm``/``-wal`` sidecar files are absent needs to (re)create the ``-shm``
    shared-memory file, which a ``mode=ro`` open cannot do — it fails with
    ``OperationalError: unable to open database file``. ``immutable=1`` opens
    without a ``-shm`` and snapshots correctly.
    """
    source = sqlite3.connect(f"file:{src}?immutable=1", uri=True)
    try:
        dest = sqlite3.connect(dst)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _seed_isolated_db(data_dir: Path, *, canonical_db: Path, isolation_root: Path) -> None:
    """Seed an auto-isolated worktree dir from a consistent canonical snapshot.

    Only seeds paths inside ``isolation_root`` — a primary clone or an explicit
    ``XDG_DATA_HOME`` sandbox is never under it, so it is never seeded
    regardless of how this is called. The snapshot is written to a temp file
    in the target dir and published with a same-filesystem atomic rename, so a
    reader never observes a partial DB even under concurrent startup. The
    exclusive lock around the rename is an optimisation that prevents two
    startups from redundantly re-doing the snapshot; correctness rests on the
    atomic rename, not the lock.
    """
    try:
        data_dir.resolve().relative_to(isolation_root.resolve())
    except ValueError:
        return
    if not canonical_db.exists():
        return
    target = data_dir / "db.sqlite3"
    if target.exists():
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(data_dir / ".seed.lock"):
        if target.exists():
            return
        fd, tmp_name = tempfile.mkstemp(prefix=".seed-", suffix=".sqlite3", dir=data_dir)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            _sqlite_snapshot(canonical_db, tmp_path)
            tmp_path.replace(target)
        finally:
            tmp_path.unlink(missing_ok=True)


def seed_isolated_db(data_dir: Path) -> None:
    """Module-level binding of :func:`_seed_isolated_db` to the real canonical DB."""
    _seed_isolated_db(
        data_dir,
        canonical_db=TRUE_CANONICAL_DB,
        isolation_root=_worktree_isolation_root(Path.home()),
    )


_RESOLVED = resolve_data_dir(env=dict(os.environ), home=Path.home(), repo_root=_code_repo_root())
DATA_DIR = _RESOLVED.path
DATA_DIR_AUTO_ISOLATED = _RESOLVED.auto_isolated
CANONICAL_DB = DATA_DIR / "db.sqlite3"


def get_data_dir(namespace: str) -> Path:
    data_dir = DATA_DIR / namespace
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def expected_db_for_repo(repo_root: Path, *, env: dict[str, str], home: Path) -> Path:
    """The control-DB path that code resident in *repo_root* resolves to.

    Deterministic from the on-disk location alone — the same function the
    process uses at import time (:func:`resolve_data_dir`), just parameterised
    by an explicit ``repo_root`` instead of ``_code_repo_root()``. A primary
    clone yields the canonical DB; a git worktree yields its sibling
    auto-isolated DB; an explicit ``XDG_DATA_HOME`` sandbox yields that
    sandbox's DB. This is the anchor for the cross-DB guard (#779): a
    ticket's lifecycle/ship state lives in exactly one DB — the one its
    worktree's resident code would resolve to — regardless of the CWD the
    ``t3`` command happens to run from.
    """
    return resolve_data_dir(env=env, home=home, repo_root=repo_root).path / "db.sqlite3"


def find_overlay_db(name: str, project_path: str) -> Path | None:
    for candidate in (Path(project_path).expanduser() / "db.sqlite3", DATA_DIR / name / "db.sqlite3"):
        if candidate.is_file():
            return candidate
    return None


def find_stale_dbs(data_dir: Path, *, canonical: Path) -> Iterator[Path]:
    """Yield ``db.sqlite3`` files inside ``data_dir`` that aren't ``canonical``.

    Walks recursively under ``data_dir`` so any legacy namespaced layout
    (``data_dir/<name>/db.sqlite3``) surfaces. The canonical path is skipped.
    Auto-isolated worktree DBs live under the sibling ``teatree-worktrees``
    root, never under ``data_dir``, so they are structurally excluded here.
    Used by both the settings warning and the ``t3 doctor`` check.
    """
    if not data_dir.is_dir():
        return
    canonical = canonical.resolve()
    for candidate in data_dir.glob("**/db.sqlite3"):
        if candidate.resolve() == canonical:
            continue
        yield candidate
