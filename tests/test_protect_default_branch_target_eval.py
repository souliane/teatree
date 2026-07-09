"""Eval matrix for the protected-branch Write/Edit gate (#126).

The gate-over-deny lockout this guards against: the gate keyed on the
branch of whatever repo ENCLOSES the target file's parent dir
(``git -C <parent> rev-parse``). ``git`` walks UP the directory tree to
the nearest ``.git``, so a Write to the agent memory dir
(``~/.claude/projects/*/memory/*.md``) — which is itself inside a git
repo on ``main`` (the agent-config dotfiles repo) — was blocked as "on
protected branch 'main'", even though the memory file is agent scratch
state, never protected source.

The sharpened scope: the gate protects ONLY teatree-MANAGED source
repos (teatree core + the active overlay's registered repos, read from
the DB-home ``overlays`` ConfigSetting row — ``workspace_repos`` /
``frontend_repos`` / ``public_repos`` plus each overlay ``path``). It
keys on the TARGET FILE's repo, never on the cwd's branch, and never on
"is the file in ANY git repo". An unmanaged git repo on ``main`` is
allowed; the agent memory dir is explicitly exempt even when git-tracked.

Scenario matrix:

* Write to a tracked file in a teatree-MANAGED repo on main → BLOCK;
* Write to the agent memory dir (git-tracked, on main) → ALLOW;
* Write to a file in an UNMANAGED git repo on main → ALLOW;
* Write to a file OUTSIDE any git repo → ALLOW;
* a write on a feature branch (managed repo) → ALLOW;
* fails OPEN on a broken git env → ALLOW.
"""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_protect_default_branch

_GIT = shutil.which("git")


def _seed_overlays_db(path: Path, overlays: dict[str, object]) -> None:
    """Seed the DB-home ``overlays`` ConfigSetting row (global scope) into a real sqlite DB."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps(overlays),),
        )
        conn.commit()
    finally:
        conn.close()


# A remote whose slug is teatree-managed (core's own slug is always
# managed per ``_overlay_managed_repo_signals``).
_MANAGED_REMOTE = "git@github.com:souliane/teatree.git"
# A remote that no overlay registers — an ordinary unrelated repo.
_UNMANAGED_REMOTE = "git@github.com:someone-else/unrelated-tool.git"


def _git(repo: Path, *args: str) -> None:
    assert _GIT is not None
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run([_GIT, "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _repo_on_branch(root: Path, branch: str, *, remote: str, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    assert _GIT is not None
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run([_GIT, "init", "-b", branch], cwd=repo, check=True, capture_output=True, env=env)
    _git(repo, "remote", "add", "origin", remote)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")
    return repo


def _write(file_path: str) -> dict[str, object]:
    return {"tool_name": "Write", "tool_input": {"file_path": file_path}}


@pytest.fixture(autouse=True)
def _no_overlay_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin HOME to an empty dir and clear the config-DB env so only teatree-core's own slug is managed.

    Keeps the managed-repo classification deterministic — it does not
    pick up the developer's real DB-home overlay repos.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


@pytest.mark.skipif(_GIT is None, reason="git not on PATH")
class TestProtectedBranchManagedScoping:
    """The gate blocks only teatree-managed source on a protected branch."""

    def test_managed_repo_tracked_file_on_main_is_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_on_branch(tmp_path, "main", remote=_MANAGED_REMOTE)
        blocked = handle_protect_default_branch(_write(str(repo / "tracked.py")))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "protected branch" in out["permissionDecisionReason"]

    def test_managed_repo_new_source_file_on_main_is_blocked(self, tmp_path: Path) -> None:
        repo = _repo_on_branch(tmp_path, "main", remote=_MANAGED_REMOTE)
        assert handle_protect_default_branch(_write(str(repo / "newfile.py"))) is True

    def test_unmanaged_repo_on_main_is_allowed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # A git repo on `main` that no overlay registers must NOT block —
        # the gate guards teatree-managed source only, not every repo.
        repo = _repo_on_branch(tmp_path, "main", remote=_UNMANAGED_REMOTE)
        blocked = handle_protect_default_branch(_write(str(repo / "tracked.py")))
        assert blocked is False, "an unmanaged repo on main must not be blocked"
        assert capsys.readouterr().out == ""

    def test_memory_dir_in_managed_repo_on_main_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The reproduction: the agent memory dir is git-tracked and on
        # `main`, inside a teatree-managed repo — it is still agent
        # scratch state, never protected source.
        repo = _repo_on_branch(tmp_path, "main", remote=_MANAGED_REMOTE)
        memory = repo / ".claude" / "projects" / "proj" / "memory"
        memory.mkdir(parents=True)
        blocked = handle_protect_default_branch(_write(str(memory / "MEMORY.md")))
        assert blocked is False, "agent memory dir must be exempt even in a managed repo on main"
        assert capsys.readouterr().out == ""

    def test_codex_memory_dir_is_allowed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo_on_branch(tmp_path, "main", remote=_MANAGED_REMOTE)
        memory = repo / ".codex" / "projects" / "x" / "memory"
        memory.mkdir(parents=True)
        assert handle_protect_default_branch(_write(str(memory / "topic.md"))) is False
        assert capsys.readouterr().out == ""

    def test_file_outside_any_repo_is_allowed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        target = tmp_path / "scratch" / "note.txt"
        target.parent.mkdir()
        assert handle_protect_default_branch(_write(str(target))) is False
        assert capsys.readouterr().out == ""

    def test_managed_repo_on_feature_branch_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_on_branch(tmp_path, "feature-x", remote=_MANAGED_REMOTE)
        assert handle_protect_default_branch(_write(str(repo / "tracked.py"))) is False
        assert capsys.readouterr().out == ""

    def test_broken_git_env_fails_open(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_protect_default_branch(_write(str(tmp_path / "ghost" / "file.txt"))) is False
        assert capsys.readouterr().out == ""

    def test_managed_via_overlay_path_is_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A repo with no managed slug, but whose working tree is under an
        # overlay's registered ``path``, is managed too.
        repo = _repo_on_branch(tmp_path, "main", remote=_UNMANAGED_REMOTE, name="overlay-repo")
        db = tmp_path / "config.sqlite3"
        _seed_overlays_db(db, {"x": {"path": str(repo)}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert handle_protect_default_branch(_write(str(repo / "tracked.py"))) is True
