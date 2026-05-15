"""Tests for ``teatree.paths`` helpers."""

from pathlib import Path

import pytest

from teatree.paths import CanonicalDBFromWorktreeError, find_stale_dbs, resolve_data_dir, running_from_worktree


def _make_repo(root: Path, *, worktree: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    if worktree:
        git.write_text("gitdir: /somewhere/.git/worktrees/x\n", encoding="utf-8")
    else:
        git.mkdir()
    return root


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
        assert resolve_data_dir(env={}, home=home, repo_root=repo) == home / ".local" / "share" / "teatree"

    def test_primary_clone_respects_xdg(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "main", worktree=False)
        xdg = tmp_path / "xdg"
        got = resolve_data_dir(env={"XDG_DATA_HOME": str(xdg)}, home=home, repo_root=repo)
        assert got == xdg / "teatree"

    def test_worktree_auto_isolates_deterministically(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        first = resolve_data_dir(env={}, home=home, repo_root=repo)
        second = resolve_data_dir(env={}, home=home, repo_root=repo)
        canonical = home / ".local" / "share" / "teatree"
        assert first == second
        assert first.parent == canonical / "_worktrees"
        assert first != canonical
        assert first / "db.sqlite3" != canonical / "db.sqlite3"

    def test_worktree_isolation_differs_per_repo(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        a = _make_repo(tmp_path / "wt-a", worktree=True)
        b = _make_repo(tmp_path / "wt-b", worktree=True)
        assert resolve_data_dir(env={}, home=home, repo_root=a) != resolve_data_dir(env={}, home=home, repo_root=b)

    def test_worktree_respects_explicit_sandbox_xdg(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        sandbox = tmp_path / "sbx"
        got = resolve_data_dir(env={"XDG_DATA_HOME": str(sandbox)}, home=home, repo_root=repo)
        assert got == sandbox / "teatree"

    def test_worktree_pointing_at_true_canonical_hard_fails(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "wt", worktree=True)
        canonical_xdg = home / ".local" / "share"
        with pytest.raises(CanonicalDBFromWorktreeError):
            resolve_data_dir(env={"XDG_DATA_HOME": str(canonical_xdg)}, home=home, repo_root=repo)


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
