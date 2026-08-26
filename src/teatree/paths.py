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

DB_FILENAME = "db.sqlite3"

#: Env name carrying the DIRECTORY holding the canonical control DB. The
#: containerized stack points it at a named-volume mount; the ``-wal``/``-shm``
#: sidecars must land beside the file, which is why the volume is the directory
#: and not the file.
CONTROL_DB_DIR_ENV = "T3_CONTROL_DB_DIR"

#: Where the canonical control DB lives when the env names nothing. Deliberately
#: NOT derived from ``home``: the path must be IDENTICAL inside and outside the
#: container while existing only inside it, so a host process resolves a
#: directory it cannot create and fails loudly instead of quietly opening a
#: second database. That is the enforcement — ``t3`` runs in the container.
DEFAULT_CONTROL_DB_DIR = Path("/var/lib/teatree/control-db")


def control_db_dir(env: Mapping[str, str]) -> Path:
    """The directory holding the canonical control DB — the named-volume mount."""
    override = env.get(CONTROL_DB_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_CONTROL_DB_DIR


def canonical_db_in(data_dir: Path, *, env: Mapping[str, str]) -> Path:
    """The control DB file for *data_dir* — the named volume for the REAL install only.

    A data dir that IS the machine's canonical one resolves into the control-DB
    volume, taking the database off the host bind mount that host processes
    contend for. Every other data dir — a per-worktree isolated dir, an explicit
    ``XDG_DATA_HOME`` sandbox, a test's ``tmp_path`` home — keeps its database
    beside itself, because those are private copies with no second writer and no
    volume to reach.
    """
    if data_dir == _TRUE_CANONICAL_DATA_DIR:
        return control_db_dir(env) / DB_FILENAME
    return data_dir / DB_FILENAME


#: The one control DB the installed ``t3`` and the live loop operate on. Every
#: ``t3 <ov> <cmd>`` proxies through the main clone (a ``.git`` *dir*), which
#: resolves here. Worktree-resident ``uv run manage.py`` resolves to an
#: isolated sibling DB instead — the #779 cross-DB mismatch. Public so the
#: lifecycle/ship guard can name it in the refusal message.
TRUE_CANONICAL_DB = canonical_db_in(_TRUE_CANONICAL_DATA_DIR, env=os.environ)

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


def _nearest_git_entry(repo_root: Path) -> Path | None:
    """The first ``.git`` entry at *repo_root* or up its parent chain, if any."""
    for candidate in (repo_root, *repo_root.parents):
        git = candidate / ".git"
        if git.is_file() or git.is_dir():
            return git
    return None


def running_from_worktree(repo_root: Path) -> bool:
    """A git worktree has a ``.git`` *file*; a primary clone has a ``.git`` *dir*.

    Resolved against the NEAREST ``.git`` entry walking up from *repo_root*: a
    vendored core tree (``<fork>/vendor/teatree``) has no ``.git`` of its own,
    so the fork checkout that CONTAINS it decides. Without the walk-up, a git
    worktree of a vendoring fork read as "not a worktree" and its resident code
    resolved onto the true canonical DB — exactly the unmerged-migration
    corruption this module exists to prevent. No ``.git`` anywhere up the chain
    (an installed site-packages tree) stays "not a worktree".
    """
    git = _nearest_git_entry(repo_root)
    return git is not None and git.is_file()


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


def teatree_source_root() -> Path:
    """The directory holding teatree's own ``src/teatree`` package tree.

    Derived from this module's own on-disk location, so it is layout-independent:
    the repo root in a plain clone, and ``<repo>/vendor/teatree`` in a fork that
    vendors core. Deliberately NOT resolved through ``git rev-parse
    --show-toplevel`` — a vendored subtree has no ``.git`` of its own, so git
    reports the OUTER repo and the two layouts become indistinguishable. Any
    caller that needs "where does the teatree package live" must use this;
    callers that need the surrounding git working tree resolve it from here.
    """
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


#: Written inside each auto-isolated env dir, naming the checkout that owns it.
#: :func:`isolated_slug` is a one-way hash, so the resolver's checkout -> dir
#: mapping cannot be walked backwards: a reaper holding only the dir can at best
#: INFER which checkouts are still alive and hope its inference covers the same
#: population the resolver mints for. The stamp closes that by recording the
#: owner on the way in, so liveness becomes a fact the dir carries (#3852).
OWNER_STAMP_NAME = "owner-checkout.path"


@dataclass(frozen=True, slots=True)
class IsolatedEnvDir:
    """One auto-isolated per-worktree env dir, and the owner stamp naming its checkout.

    The stamp is what makes the slug mapping invertible. Without it a reaper can
    only ask "is this slug in the set of checkouts I know about?" — an answer that
    is wrong whenever its population is narrower than the resolver's, which is the
    defect #3852 fixes. With it the dir answers "this exact checkout owns me", and
    liveness is a one-line existence check needing no population agreement at all.
    """

    path: Path

    @property
    def owner(self) -> Path | None:
        """The checkout stamped into this dir, or ``None`` when unstamped/unreadable.

        ``None`` is "predates the stamp or could not be read", never "no owner" — a
        caller must fall back to its other evidence rather than read the absence as
        proof the dir is dead.
        """
        try:
            raw = (self.path / OWNER_STAMP_NAME).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return Path(raw) if raw else None

    def open_for(self, repo_root: Path) -> None:
        """Bring this dir into existence owned by *repo_root*, stamped at birth (#3872).

        THE birth seam: an auto-isolated env dir comes into existence here, so the
        stamp is written BEFORE the dir holds anything else and no code path can mint
        an unstamped one. Ordering is the whole point — seeding copies a
        multi-gigabyte control DB, and a startup that died mid-copy left a dir on disk
        that a reaper could only judge by inference. The dir's owner is not evidence a
        later pass has to rediscover: it is a fact the dir carries from its first byte.
        """
        self.stamp_owner(repo_root)
        seed_isolated_db(self.path)

    def stamp_owner(self, repo_root: Path) -> None:
        """Record *repo_root* as this dir's owning checkout.

        Idempotent and crash-proof: an unchanged stamp is left alone, and any write
        error is swallowed — this runs at settings import, where a read-only or full
        filesystem must never turn a diagnostic aid into a failure to start.
        """
        stamp = self.path / OWNER_STAMP_NAME
        try:
            if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == str(repo_root):
                return
            self.path.mkdir(parents=True, exist_ok=True)
            stamp.write_text(f"{repo_root}\n", encoding="utf-8")
        except OSError:
            return


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
        db = canonical_db_in(resolved.path, env=self.env)
        return ControlDbResolution(db, isolated=resolved.auto_isolated, reason=reason)

    def primary_data_dir(self) -> Path:
        """The PRIMARY data dir — the bind-mounted tree the DB no longer lives in.

        Backups, the handover mirror, the mode override and every other
        host-visible artifact resolve here, NOT from the control DB's parent: the
        two are different filesystems now, so a caller that wanted the data dir
        and reached for ``<db>.parent`` would silently address the volume.

        Resolved against the primary-clone sentinel, so it is also deliberately NOT
        the auto-isolated per-worktree :data:`DATA_DIR`: an artifact describing the
        *operator* rather than a checkout (the live-presence heartbeat) has exactly
        one instance, and giving one keyboard two heartbeats is how a fast path and
        a Django path come to disagree about the same fact.
        """
        home = self.home if self.home is not None else Path.home()
        return resolve_data_dir(env=dict(self.env), home=home, repo_root=_PRIMARY_CLONE_SENTINEL).path

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
    """Consistent point-in-time copy INCLUDING commits still living in the ``-wal``.

    ``?mode=ro`` is tried first because it is the case that matters. A live
    WAL-mode DB keeps every commit in its ``-wal`` until a checkpoint folds it
    back into the main file, and only a connection that READS the WAL sees
    them. ``?immutable=1`` does not: it promises SQLite the file cannot change,
    so SQLite skips locking *and* ignores the ``-wal`` entirely. Snapshotting a
    live DB that way silently drops every transaction since the last checkpoint
    and can tear pages under a concurrent writer — which is how an
    unrestorable "backup" gets produced. ``mode=ro`` also takes real read
    locks, so the snapshot is a consistent point in time rather than a smear.

    ``immutable=1`` stays as the FALLBACK, for the case it was actually added
    for: a cold artifact whose ``-shm`` is absent and cannot be created (a
    read-only file or directory), where ``mode=ro`` fails with
    ``OperationalError: unable to open database file``. A cold artifact has no
    uncheckpointed WAL to lose, so the fallback is lossless exactly where it
    applies. The probe query is what forces the real open — ``connect`` alone
    is lazy, so a missing ``-shm`` would otherwise surface later, mid-backup.

    The source is never opened read-write, so this stays legal on a host whose
    control DB the containerized stack owns (:mod:`teatree.db.boundary`).
    """
    # ``as_uri`` percent-encodes a path holding a URI-special character (space,
    # ``%``, ``?``, ``#``) instead of malforming the URI into a different open.
    base_uri = src.absolute().as_uri()
    source = sqlite3.connect(f"{base_uri}?mode=ro", uri=True)
    try:
        source.execute("SELECT 1").fetchone()
    except sqlite3.OperationalError:
        source.close()
        source = sqlite3.connect(f"{base_uri}?immutable=1", uri=True)
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
    target = data_dir / DB_FILENAME
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


#: The repo root the running teatree code lives in — the checkout that owns its
#: env dir, and so the value stamped into it. Resolved through
#: :func:`teatree_source_root`, the one layout-independent spelling of this rule, so a
#: vendored core and a plain clone answer it the same way.
CODE_REPO_ROOT = teatree_source_root()

_RESOLVED = resolve_data_dir(env=dict(os.environ), home=Path.home(), repo_root=CODE_REPO_ROOT)
DATA_DIR = _RESOLVED.path
DATA_DIR_AUTO_ISOLATED = _RESOLVED.auto_isolated
CANONICAL_DB = canonical_db_in(DATA_DIR, env=os.environ)


def data_dir_root() -> Path:
    """The single root for teatree's on-disk data.

    ``T3_DATA_DIR`` wins — the explicit override every gate already honours — and
    otherwise :data:`DATA_DIR`, the XDG-resolved (and possibly worktree-isolated)
    canonical dir. :data:`DATA_DIR` deliberately does NOT read ``T3_DATA_DIR``
    (:func:`resolve_data_dir` keys on ``XDG_DATA_HOME``), so a caller that must honour
    the override needs this two-step and must not reach for :data:`DATA_DIR` directly.

    Resolved per call rather than at import, so a process that sets ``T3_DATA_DIR``
    after teatree is imported still gets the override.
    """
    override = os.environ.get("T3_DATA_DIR")
    return Path(override) if override else DATA_DIR


#: The positions a control database actually occupies under the data dir: beside the
#: root, and one namespaced level down (the legacy ``data_dir/<name>/`` layout). NOT
#: a full ``**`` walk — the data dir also roots the backup tree, 62k entries on a
#: real install, and walking it over a bind mount costs 20-115s for files no live
#: process holds. Both measured offenders sat at the root; the bound keeps a level
#: of margin at 0.14s.
_CONTROL_DB_ARTIFACT_GLOBS = ("{name}*", "*/{name}*")


class PathHelpers:
    """Module-level helpers grouped so the module keeps a readable public surface."""

    @staticmethod
    def core_repo_root(*, root: Path | None = None) -> Path | None:
        """*root* (default :func:`teatree_source_root`) when it is a core checkout, else ``None``.

        A non-editable install resolves the arithmetic onto a site-packages tree
        carrying neither marker, so a caller can tell "the destination is absent from
        the tree" from "there is no tree to read" and degrade loudly instead of
        classifying every core path as absent.
        """
        base = root if root is not None else teatree_source_root()
        markers = (base / "src" / "teatree" / "__init__.py", base / "pyproject.toml")
        return base if all(marker.exists() for marker in markers) else None

    @staticmethod
    def get_data_dir(namespace: str) -> Path:
        data_dir = DATA_DIR / namespace
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    @staticmethod
    def expected_db_for_repo(repo_root: Path, *, env: dict[str, str], home: Path) -> Path:
        """The control-DB path that code resident in *repo_root* resolves to.

        Deterministic from the on-disk location alone — the same function the
        process uses at import time (:func:`resolve_data_dir`), just parameterised
        by an explicit ``repo_root`` instead of ``teatree_source_root()``. A primary
        clone yields the canonical DB; a git worktree yields its sibling
        auto-isolated DB; an explicit ``XDG_DATA_HOME`` sandbox yields that
        sandbox's DB. This is the anchor for the cross-DB guard (#779): a
        ticket's lifecycle/ship state lives in exactly one DB — the one its
        worktree's resident code would resolve to — regardless of the CWD the
        ``t3`` command happens to run from.
        """
        return canonical_db_in(resolve_data_dir(env=env, home=home, repo_root=repo_root).path, env=env)

    @staticmethod
    def find_overlay_db(name: str, project_path: str) -> Path | None:
        for candidate in (Path(project_path).expanduser() / DB_FILENAME, DATA_DIR / name / DB_FILENAME):
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
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

    @staticmethod
    def find_control_db_artifacts(data_dir: Path, *, canonical: Path) -> Iterator[Path]:
        """Yield every file under ``data_dir`` that IS, or once was, a control database.

        Wider than :func:`find_stale_dbs` in the two directions a descriptor survives.
        Matching is by PREFIX because a RENAME does not close the descriptors held on a
        file: the writer a ``db.sqlite3.precorrupt-<stamp>`` rename was meant to retire
        keeps writing to the same inode under the new name, which the exact-name sweep
        cannot see. And the ``-wal``/``-shm`` sidecars are the same database, so a
        process holding the write-ahead log open for writing is a writer.

        ``canonical`` is excluded. On a migrated install it is not under ``data_dir`` at
        all — it lives in the control-DB volume, which has no host path — so everything
        yielded here is a copy the host CAN name, and therefore CAN hold open.
        """
        if not data_dir.is_dir():
            return
        canonical = canonical.resolve()
        seen: set[Path] = set()
        for pattern in _CONTROL_DB_ARTIFACT_GLOBS:
            for candidate in sorted(data_dir.glob(pattern.format(name=DB_FILENAME))):
                if candidate in seen or not candidate.is_file() or candidate.resolve() == canonical:
                    continue
                seen.add(candidate)
                yield candidate


get_data_dir = PathHelpers.get_data_dir
expected_db_for_repo = PathHelpers.expected_db_for_repo
find_overlay_db = PathHelpers.find_overlay_db
find_stale_dbs = PathHelpers.find_stale_dbs
find_control_db_artifacts = PathHelpers.find_control_db_artifacts
