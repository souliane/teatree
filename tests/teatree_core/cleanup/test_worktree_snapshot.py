"""Django-free ``capture_worktree_snapshot`` primitive (#1764).

Drives the SubagentStop-shared capture against a real bare-remote git topology
under ``tmp_path``. No Django: the snapshot helper depends only on
``teatree.utils.git`` so the bare-``python3`` SubagentStop hook can call it.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.core.worktree_snapshot import capture_worktree_snapshot
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _recovery_dirs(temp_root: Path) -> list[Path]:
    return sorted(p for p in temp_root.iterdir() if p.is_dir() and p.name.startswith("t3-recover-"))


class _GitTopology:
    def __init__(self, tmp_path: Path) -> None:
        self.temp_root = tmp_path / "systmp"
        self.temp_root.mkdir()
        self.remote = tmp_path / "remote.git"
        subprocess.run(
            [_GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        self.repo_main = tmp_path / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        (self.repo_main / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        self.branch = "feat-1764-x"
        self.wt_path = tmp_path / "wt" / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def push_branch_to_main(self) -> None:
        _run_git("push", "-q", "origin", f"{self.branch}:main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)


@pytest.fixture
def topo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _GitTopology:
    t = _GitTopology(tmp_path)
    monkeypatch.setattr("teatree.core.worktree_snapshot.tempfile.gettempdir", lambda: str(t.temp_root))
    return t


def _capture(topo: _GitTopology) -> Path | None:
    return capture_worktree_snapshot(topo.repo_main, str(topo.wt_path), branch=topo.branch, label="1764")


def test_dirty_worktree_captures_restorable_artifact(topo: _GitTopology) -> None:
    (topo.wt_path / "base.txt").write_text("base\nDIRTY\n", encoding="utf-8")
    (topo.wt_path / "newfile.txt").write_text("brand new\n", encoding="utf-8")

    rec = _capture(topo)

    assert rec is not None
    assert (rec / "branch.bundle").is_file()
    assert (rec / "working-tree.diff").is_file()
    restore = topo.temp_root / "restore"
    subprocess.run(
        [_GIT, "clone", "-q", "-b", topo.branch, str(rec / "branch.bundle"), str(restore)],
        check=True,
        capture_output=True,
        cwd=str(topo.temp_root),
        env=_clean_env(),
    )
    subprocess.run(
        [_GIT, "-C", str(restore), "apply", str(rec / "working-tree.diff")],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    assert (restore / "base.txt").read_text(encoding="utf-8") == "base\nDIRTY\n"
    assert (restore / "newfile.txt").read_text(encoding="utf-8") == "brand new\n"


def test_unpushed_commits_captured_when_working_tree_clean(topo: _GitTopology) -> None:
    (topo.wt_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    _run_git("add", "-A", cwd=topo.wt_path)
    _run_git("commit", "-q", "-m", "feat: unpushed", cwd=topo.wt_path)

    rec = _capture(topo)

    assert rec is not None
    assert (rec / "branch.bundle").is_file()


def test_clean_and_pushed_worktree_is_noop(topo: _GitTopology) -> None:
    topo.push_branch_to_main()

    rec = _capture(topo)

    assert rec is None
    assert _recovery_dirs(topo.temp_root) == []


def test_missing_worktree_dir_is_noop(topo: _GitTopology) -> None:
    rec = capture_worktree_snapshot(topo.repo_main, "/nonexistent/path", branch=topo.branch, label="1764")
    assert rec is None


def test_survives_git_worktree_remove_of_the_captured_tree(topo: _GitTopology) -> None:
    (topo.wt_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    _run_git("add", "-A", cwd=topo.wt_path)
    _run_git("commit", "-q", "-m", "feat: unpushed", cwd=topo.wt_path)

    rec = _capture(topo)
    assert rec is not None

    # The bundle is self-contained — removing the worktree must not invalidate it.
    _run_git("worktree", "remove", "--force", str(topo.wt_path), cwd=topo.repo_main)
    assert not topo.wt_path.exists()
    restore = topo.temp_root / "restore-after-remove"
    subprocess.run(
        [_GIT, "clone", "-q", "-b", topo.branch, str(rec / "branch.bundle"), str(restore)],
        check=True,
        capture_output=True,
        cwd=str(topo.temp_root),
        env=_clean_env(),
    )
    log = subprocess.run(
        [_GIT, "-C", str(restore), "log", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout
    assert "feat: unpushed" in log
