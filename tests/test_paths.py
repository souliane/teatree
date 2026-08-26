"""Tests for ``teatree.paths`` helpers."""

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree import paths
from teatree.paths import (
    CanonicalDBFromWorktreeError,
    ResolvedDataDir,
    _seed_isolated_db,
    _sqlite_snapshot,
    _worktree_isolation_root,
    find_control_db_artifacts,
    find_overlay_db,
    find_stale_dbs,
    resolve_data_dir,
    running_from_worktree,
)


def _make_repo(root: Path, *, worktree: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    if worktree:
        git.write_text("gitdir: /somewhere/.git/worktrees/x\n", encoding="utf-8")
    else:
        git.mkdir()
    return root


def _make_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO marker (note) VALUES ('canonical')")
        conn.commit()
    finally:
        conn.close()


def _make_wal_sqlite_db(path: Path) -> None:
    """A WAL-mode DB whose ``-shm``/``-wal`` sidecar files are absent.

    ``PRAGMA journal_mode=WAL`` persists in the file header (bytes 18-19 become
    ``2,2``); after a checkpoint+close and reaping the sidecar files, only the main
    db file remains, still flagged WAL. Opening it WAL-mode requires (re)creating
    the ``-shm`` shared-memory file — which a ``mode=ro`` open cannot do, so it
    raises ``OperationalError``. This is the on-disk shape an auto-isolated
    worktree seed snapshots from.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO marker (note) VALUES ('canonical')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for companion in ("-wal", "-shm"):
        Path(str(path) + companion).unlink(missing_ok=True)


class TestRunningFromWorktree:
    def test_git_file_is_worktree(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "wt", worktree=True)
        assert running_from_worktree(repo) is True

    def test_git_dir_is_primary_clone(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "main", worktree=False)
        assert running_from_worktree(repo) is False

    def test_no_git_is_not_worktree(self, tmp_path: Path) -> None:
        (tmp_path / "bare").mkdir()
        assert running_from_worktree(tmp_path / "bare") is False

    def test_vendored_core_inside_a_fork_worktree_is_a_worktree(self, tmp_path: Path) -> None:
        # A vendoring fork's core tree has no .git of its own — the fork
        # checkout that CONTAINS it decides. Read as "not a worktree", the
        # vendored code of a fork WORKTREE resolved onto the true canonical DB.
        fork = _make_repo(tmp_path / "fork-wt", worktree=True)
        vendored = fork / "vendor" / "teatree"
        vendored.mkdir(parents=True)
        assert running_from_worktree(vendored) is True

    def test_vendored_core_inside_a_fork_primary_clone_is_primary(self, tmp_path: Path) -> None:
        fork = _make_repo(tmp_path / "fork", worktree=False)
        vendored = fork / "vendor" / "teatree"
        vendored.mkdir(parents=True)
        assert running_from_worktree(vendored) is False


class TestResolveDataDir:
    def test_primary_clone_uses_canonical(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "main", worktree=False)
        resolved = resolve_data_dir(env={}, home=home, repo_root=repo)
        assert resolved == ResolvedDataDir(home / ".local" / "share" / "teatree", auto_isolated=False)

    def test_primary_clone_respects_xdg(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "main", worktree=False)
        xdg = tmp_path / "xdg"
        resolved = resolve_data_dir(env={"XDG_DATA_HOME": str(xdg)}, home=home, repo_root=repo)
        assert resolved == ResolvedDataDir(xdg / "teatree", auto_isolated=False)

    def test_worktree_auto_isolates_deterministically(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        first = resolve_data_dir(env={}, home=home, repo_root=repo)
        second = resolve_data_dir(env={}, home=home, repo_root=repo)
        assert first == second
        assert first.auto_isolated is True
        assert first.path.parent == _worktree_isolation_root(home)

    def test_auto_isolated_path_is_not_under_canonical_data_dir(self, tmp_path: Path) -> None:
        """H1 regression: isolated DBs must not live under the scanned canonical dir."""
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        resolved = resolve_data_dir(env={}, home=home, repo_root=repo)
        canonical_data_dir = home / ".local" / "share" / "teatree"
        with pytest.raises(ValueError, match=r"subpath|does not start with"):
            resolved.path.resolve().relative_to(canonical_data_dir.resolve())

    def test_worktree_isolation_differs_per_repo(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        a = _make_repo(tmp_path / "wt-a", worktree=True)
        b = _make_repo(tmp_path / "wt-b", worktree=True)
        assert resolve_data_dir(env={}, home=home, repo_root=a) != resolve_data_dir(env={}, home=home, repo_root=b)

    def test_worktree_respects_explicit_sandbox_xdg(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        sandbox = tmp_path / "sbx"
        resolved = resolve_data_dir(env={"XDG_DATA_HOME": str(sandbox)}, home=home, repo_root=repo)
        assert resolved == ResolvedDataDir(sandbox / "teatree", auto_isolated=False)

    def test_worktree_pointing_at_true_canonical_hard_fails(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        canonical_xdg = home / ".local" / "share"
        with pytest.raises(CanonicalDBFromWorktreeError):
            resolve_data_dir(env={"XDG_DATA_HOME": str(canonical_xdg)}, home=home, repo_root=repo)

    def test_vendored_core_in_a_fork_worktree_auto_isolates(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        fork = _make_repo(tmp_path / "fork-wt", worktree=True)
        vendored = fork / "vendor" / "teatree"
        vendored.mkdir(parents=True)

        resolved = resolve_data_dir(env={}, home=home, repo_root=vendored)

        assert resolved.auto_isolated is True
        assert resolved.path.parent == _worktree_isolation_root(home)


class TestIsolatedSlug:
    def test_slug_is_deterministic_and_short(self) -> None:
        slug = paths.isolated_slug(Path("/some/worktree/org/repo"))
        assert slug == paths.isolated_slug(Path("/some/worktree/org/repo"))
        assert len(slug) == 12

    def test_distinct_repos_get_distinct_slugs(self) -> None:
        assert paths.isolated_slug(Path("/a/repo")) != paths.isolated_slug(Path("/b/repo"))

    def test_slug_matches_resolve_data_dir(self, tmp_path: Path) -> None:
        """The reaper's slug must equal the dir name the resolver actually creates."""
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        resolved = resolve_data_dir(env={}, home=home, repo_root=repo)
        assert resolved.path.name == paths.isolated_slug(repo)
        assert resolved.path.parent == _worktree_isolation_root(home)

    def test_auto_isolated_dir_ends_in_teatree_worktrees(self) -> None:
        assert paths.auto_isolated_worktrees_dir().name == "teatree-worktrees"


class TestSqliteSnapshot:
    def test_snapshots_wal_mode_db_with_absent_sidecars(self, tmp_path: Path) -> None:
        """RED-before-fix: a WAL-header DB whose dir forbids creating ``-shm``.

        With ``?mode=ro`` the snapshot open raises
        ``OperationalError: unable to open database file`` (read-only cannot
        create the ``-shm`` WAL needs). ``?immutable=1`` opens it without a
        ``-shm`` and produces a correct snapshot.
        """
        src_dir = tmp_path / "canonical"
        src = src_dir / "db.sqlite3"
        _make_wal_sqlite_db(src)
        dst = tmp_path / "snapshot.sqlite3"
        # Make the source dir read-only so a ``mode=ro`` open cannot create the
        # ``-shm`` companion — forcing the failure the fix removes.
        src_dir.chmod(0o500)
        try:
            _sqlite_snapshot(src, dst)
        finally:
            src_dir.chmod(0o700)
        conn = sqlite3.connect(dst)
        try:
            assert conn.execute("SELECT note FROM marker").fetchone()[0] == "canonical"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    def test_snapshot_includes_commits_still_resident_in_the_wal(self, tmp_path: Path) -> None:
        """A LIVE WAL DB's uncheckpointed commits must reach the snapshot.

        This is the shape every real ``db_backup`` runs against: the loop holds
        the control DB open and commits continuously, so the newest rows sit in
        the ``-wal`` until a checkpoint folds them back. An ``?immutable=1``
        source open ignores the ``-wal`` entirely, so it captured only what the
        last checkpoint had written — a backup that passes ``integrity_check``
        while silently missing days of work. Asserting on ROW COUNT rather than
        on the open mode keeps the test about the guarantee, not the mechanism.
        """
        src = tmp_path / "canonical" / "db.sqlite3"
        src.parent.mkdir(parents=True, exist_ok=True)
        writer = sqlite3.connect(src)
        try:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, note TEXT)")
            writer.execute("INSERT INTO marker (note) VALUES ('checkpointed')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Committed AFTER the last checkpoint, so these live only in the -wal.
            writer.executemany(
                "INSERT INTO marker (note) VALUES (?)",
                [(f"wal-resident-{n}",) for n in range(500)],
            )
            writer.commit()
            assert (src.parent / "db.sqlite3-wal").stat().st_size > 0, "precondition: commits are WAL-resident"

            dst = tmp_path / "snapshot.sqlite3"
            # Snapshot while the writer still holds the DB open — the live case.
            _sqlite_snapshot(src, dst)
        finally:
            writer.close()

        conn = sqlite3.connect(dst)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("SELECT count(*) FROM marker").fetchone()[0] == 501
        finally:
            conn.close()

    def test_snapshots_normal_rollback_journal_db(self, tmp_path: Path) -> None:
        """The fix must not regress the normal (non-WAL) path."""
        src = tmp_path / "canonical" / "db.sqlite3"
        _make_sqlite_db(src)
        dst = tmp_path / "snapshot.sqlite3"
        _sqlite_snapshot(src, dst)
        conn = sqlite3.connect(dst)
        try:
            assert conn.execute("SELECT note FROM marker").fetchone()[0] == "canonical"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()


class TestSeedIsolatedDb:
    def test_seeds_auto_isolated_dir_from_canonical(self, tmp_path: Path) -> None:
        canonical = tmp_path / "canonical" / "db.sqlite3"
        _make_sqlite_db(canonical)
        root = tmp_path / "teatree-worktrees"
        data_dir = root / "abc123"
        _seed_isolated_db(data_dir, canonical_db=canonical, isolation_root=root)
        seeded = data_dir / "db.sqlite3"
        assert seeded.exists()
        conn = sqlite3.connect(seeded)
        try:
            assert conn.execute("SELECT note FROM marker").fetchone()[0] == "canonical"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    def test_does_not_seed_explicit_sandbox(self, tmp_path: Path) -> None:
        """H2 regression: a path outside the isolation root is never seeded."""
        canonical = tmp_path / "canonical" / "db.sqlite3"
        _make_sqlite_db(canonical)
        root = tmp_path / "teatree-worktrees"
        sandbox = tmp_path / "sbx" / "teatree"
        _seed_isolated_db(sandbox, canonical_db=canonical, isolation_root=root)
        assert not (sandbox / "db.sqlite3").exists()

    def test_does_not_seed_when_canonical_absent(self, tmp_path: Path) -> None:
        root = tmp_path / "teatree-worktrees"
        data_dir = root / "abc123"
        _seed_isolated_db(data_dir, canonical_db=tmp_path / "missing.sqlite3", isolation_root=root)
        assert not (data_dir / "db.sqlite3").exists()

    def test_seed_is_idempotent_and_does_not_overwrite(self, tmp_path: Path) -> None:
        canonical = tmp_path / "canonical" / "db.sqlite3"
        _make_sqlite_db(canonical)
        root = tmp_path / "teatree-worktrees"
        data_dir = root / "abc123"
        _seed_isolated_db(data_dir, canonical_db=canonical, isolation_root=root)
        seeded = data_dir / "db.sqlite3"
        seeded.write_text("local-changes", encoding="utf-8")
        _seed_isolated_db(data_dir, canonical_db=canonical, isolation_root=root)
        assert seeded.read_text(encoding="utf-8") == "local-changes"

    def test_seed_leaves_no_temp_files(self, tmp_path: Path) -> None:
        canonical = tmp_path / "canonical" / "db.sqlite3"
        _make_sqlite_db(canonical)
        root = tmp_path / "teatree-worktrees"
        data_dir = root / "abc123"
        _seed_isolated_db(data_dir, canonical_db=canonical, isolation_root=root)
        leftovers = [p.name for p in data_dir.iterdir() if p.name.startswith(".seed-")]
        assert leftovers == []


class TestIsolatedEnvDirOpensStampedAtBirth:
    """An auto-isolated env dir carries its owner from its first byte (#3872).

    The stamp is the only venue-independent evidence a reaper has: a scan answers
    "did THIS venue find an owner", which the container that produced #3872 answered
    "no" for every host worktree. So the stamp must not be something a later pass
    hopes to backfill — the dir has to be born with it.
    """

    @staticmethod
    def _isolated(tmp_path: Path) -> tuple[Path, Path]:
        return tmp_path / "teatree-worktrees" / "slug", tmp_path / "checkout"

    def test_the_dir_is_stamped_before_the_control_db_is_seeded(self, tmp_path: Path) -> None:
        # RED on seed-then-stamp: seeding copies a multi-gigabyte DB, so a startup
        # that dies inside it leaves a dir on disk that no owner claims.
        data_dir, repo_root = self._isolated(tmp_path)
        stamped_when_seeding: list[Path | None] = []
        disk_full = OSError("no space left on device")

        def _record_then_fail(path: Path) -> None:
            stamped_when_seeding.append(paths.IsolatedEnvDir(path).owner)
            raise disk_full

        with patch.object(paths, "seed_isolated_db", _record_then_fail), pytest.raises(OSError, match="no space"):
            paths.IsolatedEnvDir(data_dir).open_for(repo_root)

        assert stamped_when_seeding == [repo_root], "the seed ran against an unstamped dir"

    def test_the_stamp_names_the_checkout_that_owns_the_dir(self, tmp_path: Path) -> None:
        data_dir, repo_root = self._isolated(tmp_path)

        paths.IsolatedEnvDir(data_dir).open_for(repo_root)

        assert paths.IsolatedEnvDir(data_dir).owner == repo_root


class TestStaleScanStaysCleanAfterSeed:
    def test_canonical_scan_ignores_relocated_isolated_db(self, tmp_path: Path) -> None:
        """H1 end-to-end: a seeded worktree DB must not be flagged on canonical runs."""
        home = tmp_path / "home"
        canonical_data_dir = home / ".local" / "share" / "teatree"
        canonical_db = canonical_data_dir / "db.sqlite3"
        _make_sqlite_db(canonical_db)
        repo = _make_repo(tmp_path / "wt", worktree=True)
        resolved = resolve_data_dir(env={}, home=home, repo_root=repo)
        _seed_isolated_db(
            resolved.path,
            canonical_db=canonical_db,
            isolation_root=_worktree_isolation_root(home),
        )
        assert (resolved.path / "db.sqlite3").exists()
        assert list(find_stale_dbs(canonical_data_dir, canonical=canonical_db)) == []


def test_no_stale_dbs(tmp_path: Path) -> None:
    canonical = tmp_path / "db.sqlite3"
    canonical.touch()
    assert list(find_stale_dbs(tmp_path, canonical=canonical)) == []


def test_skips_missing_data_dir(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert list(find_stale_dbs(missing, canonical=missing / "db.sqlite3")) == []


def test_finds_legacy_namespaced_layout(tmp_path: Path) -> None:
    canonical = tmp_path / "db.sqlite3"
    canonical.touch()
    stale_a = tmp_path / "teatree" / "db.sqlite3"
    stale_b = tmp_path / "dev" / "db.sqlite3"
    stale_a.parent.mkdir()
    stale_b.parent.mkdir()
    stale_a.touch()
    stale_b.touch()

    found = sorted(find_stale_dbs(tmp_path, canonical=canonical))
    assert found == sorted([stale_a, stale_b])


def test_ignores_databases_below_the_namespaced_level(tmp_path: Path) -> None:
    """A ``db.sqlite3`` deeper than one level belongs to a checkout, not to teatree.

    Every control DB teatree itself creates under the data dir sits at the root
    (:func:`resolve_data_dir`) or one namespaced level down (:func:`get_data_dir`,
    :func:`find_overlay_db`). Anything deeper arrived with a tree parked under the
    data dir — teatree's own e2e machinery checks spec repos out there — so the
    recursive walk that used to find it reported someone else's database as stale
    teatree state, and paid a full ``**`` descent of those checkouts to do it.
    """
    canonical = tmp_path / "db.sqlite3"
    canonical.touch()
    namespaced = tmp_path / "dev" / "db.sqlite3"
    namespaced.parent.mkdir()
    namespaced.touch()
    inside_a_checkout = tmp_path / "e2e-repos" / "some-repo" / "src" / "db.sqlite3"
    inside_a_checkout.parent.mkdir(parents=True)
    inside_a_checkout.touch()

    assert list(find_stale_dbs(tmp_path, canonical=canonical)) == [namespaced]


def test_neither_control_db_sweep_walks_the_whole_tree() -> None:
    """The data dir is not teatree's alone, so neither sweep may pay a ``**`` descent.

    Teatree's own e2e machinery checks spec repos out under the data dir; on the
    deployed box that is 166k files. Matching the same set with a recursive pattern
    and filtering afterwards would keep the behaviour above and lose the whole point:
    the walk cost 4.4s on the host and 274s in the container, on the SessionStart path.
    """
    recursive = [
        pattern for pattern in (*paths._CONTROL_DB_ROOT_GLOBS, *paths._CONTROL_DB_ARTIFACT_GLOBS) if "**" in pattern
    ]

    assert not recursive, f"a control-DB sweep descends the whole data dir: {recursive!r}"


class TestFindControlDbArtifacts:
    """Every file under the data dir a host process could still be writing the control DB through.

    Wider than ``find_stale_dbs`` in the two directions that actually bit: a RENAME
    does not close a descriptor, so the writer a ``.precorrupt-*`` rename was meant
    to retire keeps writing to the same inode under the new name; and the
    ``-wal``/``-shm`` sidecars ARE the database, so a writer holding the
    write-ahead log is a writer.
    """

    def test_finds_a_renamed_database_find_stale_dbs_cannot_see(self, tmp_path: Path) -> None:
        canonical = tmp_path / "control-db" / "db.sqlite3"
        canonical.parent.mkdir()
        canonical.touch()
        renamed = tmp_path / "db.sqlite3.precorrupt-inode-20260727"
        renamed.touch()

        assert list(find_stale_dbs(tmp_path, canonical=canonical)) != [renamed], "control: the narrow sweep misses it"
        assert renamed in set(find_control_db_artifacts(tmp_path, canonical=canonical))

    def test_finds_the_write_ahead_log_and_shared_memory_sidecars(self, tmp_path: Path) -> None:
        canonical = tmp_path / "control-db" / "db.sqlite3"
        canonical.parent.mkdir()
        canonical.touch()
        for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm"):
            (tmp_path / name).touch()

        found = set(find_control_db_artifacts(tmp_path, canonical=canonical))
        assert found == {tmp_path / "db.sqlite3", tmp_path / "db.sqlite3-wal", tmp_path / "db.sqlite3-shm"}

    def test_excludes_the_canonical_database_itself(self, tmp_path: Path) -> None:
        canonical = tmp_path / "db.sqlite3"
        canonical.touch()
        assert list(find_control_db_artifacts(tmp_path, canonical=canonical)) == []

    def test_finds_the_legacy_namespaced_layout_one_level_down(self, tmp_path: Path) -> None:
        canonical = tmp_path / "control-db" / "db.sqlite3"
        canonical.parent.mkdir()
        canonical.touch()
        namespaced = tmp_path / "t3-teatree" / "db.sqlite3-wal"
        namespaced.parent.mkdir()
        namespaced.touch()

        assert namespaced in set(find_control_db_artifacts(tmp_path, canonical=canonical))

    def test_does_not_walk_the_backup_tree(self, tmp_path: Path) -> None:
        # The data dir also roots backups — 62k entries on a real install, and a full
        # `**` walk of it over a bind mount measured 20-115s for files no live process
        # holds. A control database never lives that deep.
        canonical = tmp_path / "control-db" / "db.sqlite3"
        canonical.parent.mkdir()
        canonical.touch()
        archived = tmp_path / "backups" / "20260727" / "db.sqlite3"
        archived.parent.mkdir(parents=True)
        archived.touch()

        assert archived not in set(find_control_db_artifacts(tmp_path, canonical=canonical))

    def test_ignores_directories_and_unrelated_files(self, tmp_path: Path) -> None:
        canonical = tmp_path / "control-db" / "db.sqlite3"
        canonical.parent.mkdir()
        canonical.touch()
        (tmp_path / "db.sqlite3.d").mkdir()
        (tmp_path / "availability_presence").touch()

        assert list(find_control_db_artifacts(tmp_path, canonical=canonical)) == []

    def test_skips_a_missing_data_dir(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent"
        assert list(find_control_db_artifacts(missing, canonical=missing / "db.sqlite3")) == []


class TestFindOverlayDb:
    def test_returns_project_path_db_when_present(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        assert find_overlay_db("foo", str(tmp_path)) == db

    def test_falls_back_to_data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / "data"
        (data_dir / "foo").mkdir(parents=True)
        db = data_dir / "foo" / "db.sqlite3"
        db.touch()
        monkeypatch.setattr(paths, "DATA_DIR", data_dir)
        assert find_overlay_db("foo", str(tmp_path / "nonexistent")) == db

    def test_returns_none_when_no_db_anywhere(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "absent")
        assert find_overlay_db("foo", str(tmp_path / "absent")) is None


class TestCanonicalControlDbLivesInItsVolume:
    """The real install's DB resolves into the control-DB volume; every private copy does not.

    The split is what lets one machine hold a container-owned canonical database AND
    the per-worktree isolated copies that must keep sitting beside their own data dir.
    """

    def test_the_canonical_data_dir_resolves_into_the_control_db_volume(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        volume = tmp_path / "control-db"
        monkeypatch.setenv(paths.CONTROL_DB_DIR_ENV, str(volume))

        resolved = paths.canonical_db_in(paths._TRUE_CANONICAL_DATA_DIR, env=os.environ)

        assert resolved == volume / "db.sqlite3"

    def test_an_isolated_worktree_dir_keeps_its_database_beside_itself(self, tmp_path: Path) -> None:
        isolated = tmp_path / "teatree-worktrees" / "abc123"

        assert paths.canonical_db_in(isolated, env={}) == isolated / "db.sqlite3"

    def test_an_explicit_xdg_sandbox_keeps_its_database_beside_itself(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox" / "teatree"

        assert paths.canonical_db_in(sandbox, env={}) == sandbox / "db.sqlite3"

    def test_the_volume_directory_is_not_derived_from_home(self) -> None:
        # A home-derived default would exist on the host too, which is precisely what
        # must not happen: the path has to be unreachable outside the container.
        assert Path.home() not in paths.DEFAULT_CONTROL_DB_DIR.parents
        assert paths.control_db_dir({}) == paths.DEFAULT_CONTROL_DB_DIR

    def test_the_env_override_wins(self, tmp_path: Path) -> None:
        assert paths.control_db_dir({paths.CONTROL_DB_DIR_ENV: str(tmp_path)}) == tmp_path

    def test_a_blank_env_override_falls_back_to_the_default(self) -> None:
        assert paths.control_db_dir({paths.CONTROL_DB_DIR_ENV: "   "}) == paths.DEFAULT_CONTROL_DB_DIR


class TestPrimaryDataDirIsNotTheDatabasesParent:
    """Two different filesystems now, so the data dir must never be derived from the DB."""

    def test_the_primary_data_dir_stays_on_the_bind_mounted_tree(self, tmp_path: Path) -> None:
        control_db = paths.ControlDb({paths.CONTROL_DB_DIR_ENV: "/var/lib/teatree/control-db"}, home=tmp_path)

        assert control_db.primary_data_dir() == tmp_path / ".local" / "share" / "teatree"

    def test_the_data_dir_and_the_database_diverge_for_the_real_install(self) -> None:
        # The module-level constants are the REAL install's answers, resolved at import
        # from the real home — the test harness's tmp `HOME` deliberately does not reach
        # them, which is the same isolation that keeps a worktree off the canonical DB.
        assert paths.TRUE_CANONICAL_DB.parent != paths._TRUE_CANONICAL_DATA_DIR
        assert paths.TRUE_CANONICAL_DB.parent == paths.DEFAULT_CONTROL_DB_DIR
