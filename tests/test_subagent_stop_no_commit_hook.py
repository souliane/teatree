"""Tests for the SubagentStop no-commit recording hook (#1205).

An ``isolation: worktree`` sub-agent that only edits files and never commits
loses ALL its work on worktree teardown, yet the orchestrator believes work
landed — a phantom-completion source. The ``SubagentStop`` handler resolves
the sub-agent's worktree from the harness ``cwd``, runs the conservative
detector, and on a confirmed empty work branch records a
``terminated_without_commit`` signal: a durable ``<session>.no-commit`` state
file (the same seam the dispatched-sub-agent roster uses) plus a structured
stderr line. The PreCompact recovery snapshot reads that file back so the
signal survives compaction.

Integration-style: the real ``hook_router`` handler over real ``git`` under
``tmp_path`` (the project's standard pattern). The handler is registered for
the ``SubagentStop`` event and is wired in ``hooks.json``.
"""

import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import subagent_no_commit
from hooks.scripts.hook_router import _T3_TEMP_PREFIX, handle_pre_compact, handle_subagent_stop_no_commit

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    # Snapshot enrichments shell out to gh for PR state — never hit the network.
    monkeypatch.setattr(router, "_open_prs_for_repo", lambda _path: [])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=_GIT_ENV)  # noqa: S607


def _worktree_on_branch(tmp_path: Path, branch: str, *, commit: bool = False) -> Path:
    """A clone with a resolvable ``origin/main`` base, checked out on *branch*."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=_GIT_ENV)  # noqa: S607
    (seed / "README.md").write_text("init\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True, env=_GIT_ENV)  # noqa: S607

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=_GIT_ENV)  # noqa: S607
    _git(clone, "remote", "set-head", "origin", "main")
    if branch != "main":
        _git(clone, "checkout", "-q", "-b", branch)
    if commit:
        (clone / "feature.py").write_text("work\n", encoding="utf-8")
        _git(clone, "add", ".")
        _git(clone, "commit", "-q", "-m", "feature")
    return clone


def _no_commit_file(session_id: str) -> Path:
    return router.STATE_DIR / f"{session_id}.no-commit"


def _snapshot_for(session_id: str) -> Path:
    return router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-precompact.md"


class TestRecordsEmptyWorkBranch:
    def test_work_branch_zero_commits_records_signal(self, tmp_path: Path) -> None:
        clone = _worktree_on_branch(tmp_path, "1205-feat-thing")

        handle_subagent_stop_no_commit({"session_id": "sess-a", "cwd": str(clone)})

        recorded = _no_commit_file("sess-a")
        assert recorded.is_file()
        body = recorded.read_text(encoding="utf-8")
        assert "1205-feat-thing" in body
        assert str(clone) in body

    def test_signal_is_deduped_across_repeat_terminations(self, tmp_path: Path) -> None:
        clone = _worktree_on_branch(tmp_path, "1205-feat-thing")
        payload = {"session_id": "sess-a", "cwd": str(clone)}

        handle_subagent_stop_no_commit(payload)
        handle_subagent_stop_no_commit(payload)

        lines = _no_commit_file("sess-a").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

    def test_recorded_signal_surfaces_in_precompact_snapshot(self, tmp_path: Path) -> None:
        clone = _worktree_on_branch(tmp_path, "1205-feat-thing")
        handle_subagent_stop_no_commit({"session_id": "sess-a", "cwd": str(clone)})

        handle_pre_compact({"session_id": "sess-a"})

        snapshot = _snapshot_for("sess-a").read_text(encoding="utf-8")
        assert "terminated WITHOUT committing" in snapshot
        assert "1205-feat-thing" in snapshot


class TestDoesNotRecordWhenWorkLanded:
    def test_committed_branch_records_nothing(self, tmp_path: Path) -> None:
        clone = _worktree_on_branch(tmp_path, "1205-feat-thing", commit=True)

        handle_subagent_stop_no_commit({"session_id": "sess-b", "cwd": str(clone)})

        assert not _no_commit_file("sess-b").exists()


class TestDoesNotRecordReadonlyReview:
    def test_detached_review_worktree_records_nothing(self, tmp_path: Path) -> None:
        clone = _worktree_on_branch(tmp_path, "main")
        _git(clone, "checkout", "-q", "--detach", "HEAD")

        handle_subagent_stop_no_commit({"session_id": "sess-c", "cwd": str(clone)})

        assert not _no_commit_file("sess-c").exists()

    def test_base_branch_checkout_records_nothing(self, tmp_path: Path) -> None:
        clone = _worktree_on_branch(tmp_path, "main")

        handle_subagent_stop_no_commit({"session_id": "sess-c", "cwd": str(clone)})

        assert not _no_commit_file("sess-c").exists()


class TestFailsOpen:
    def test_undeterminable_git_state_records_nothing(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()

        handle_subagent_stop_no_commit({"session_id": "sess-d", "cwd": str(not_a_repo)})

        assert not _no_commit_file("sess-d").exists()

    def test_missing_cwd_is_a_clean_noop(self) -> None:
        handle_subagent_stop_no_commit({"session_id": "sess-e"})

        assert not _no_commit_file("sess-e").exists()

    def test_unexpected_error_is_contained_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A detection-path crash must never propagate out of the Stop hook."""
        clone = _worktree_on_branch(tmp_path, "1205-feat-thing")

        def _boom(*_args: object) -> None:
            raise RuntimeError

        monkeypatch.setattr(subagent_no_commit, "_record_no_commit_signal", _boom)
        # The handler must swallow the error from the recording path.
        handle_subagent_stop_no_commit({"session_id": "sess-f", "cwd": str(clone)})

        assert "no-commit detection skipped" in capsys.readouterr().err


class TestRouterWiring:
    def test_subagent_stop_event_is_registered(self) -> None:
        assert router._HANDLERS["SubagentStop"] == [handle_subagent_stop_no_commit]
