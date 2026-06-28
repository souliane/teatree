# test-path: cross-cutting
"""Source-clone resolution under ``find_clone_path``.

Real git clones under ``tmp_path``; the only mocked thing is nothing. The
missing-workspace-dir case matters because the per-overlay ``workspace_dir``
default (``~/workspace/t3-workspaces/<overlay>/``) may not exist yet on a fresh
setup — clone resolution must degrade to "no clone" rather than crash.
"""

from pathlib import Path

from teatree.core.clone_paths import find_clone_path
from tests.teatree_core.cleanup._shared import _run_git


def _init_clone(path: Path) -> None:
    path.mkdir(parents=True)
    _run_git("init", "-q", "-b", "main", cwd=path)
    _run_git("config", "user.email", "t@t", cwd=path)
    _run_git("config", "user.name", "t", cwd=path)
    _run_git("commit", "--allow-empty", "-q", "-m", "init", cwd=path)


def test_returns_none_when_workspace_dir_does_not_exist(tmp_path: Path) -> None:
    missing = tmp_path / "workspace" / "t3-workspaces" / "myoverlay"
    assert find_clone_path(missing, "myrepo") is None


def test_resolves_literal_clone(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_clone(workspace / "myrepo")
    assert find_clone_path(workspace, "myrepo") == workspace / "myrepo"


def test_resolves_namespaced_clone_by_basename(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_clone(workspace / "souliane" / "teatree")
    assert find_clone_path(workspace, "teatree") == workspace / "souliane" / "teatree"


def test_returns_none_when_no_clone_matches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert find_clone_path(workspace, "absent") is None
