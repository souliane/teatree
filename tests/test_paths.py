"""Tests for ``teatree.paths`` helpers."""

import sqlite3
from pathlib import Path

import pytest

from teatree import paths
from teatree.paths import (
    CanonicalDBFromWorktreeError,
    ResolvedDataDir,
    _seed_isolated_db,
    _sqlite_snapshot,
    _worktree_isolation_root,
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


def test_finds_nested_layouts(tmp_path: Path) -> None:
    canonical = tmp_path / "db.sqlite3"
    canonical.touch()
    nested = tmp_path / "a" / "b" / "c" / "db.sqlite3"
    nested.parent.mkdir(parents=True)
    nested.touch()

    assert list(find_stale_dbs(tmp_path, canonical=canonical)) == [nested]


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
