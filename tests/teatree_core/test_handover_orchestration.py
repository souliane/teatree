"""Directive #8 — orchestrator drives in-flight sub-agent worktrees through fast-push.

Real git clones + linked worktrees under ``tmp_path`` (a local bare ``origin``);
only the fast-pusher itself is faked (a recorder), so the enumeration + pending-work
predicates run against real ``git worktree list`` / ``git status`` output.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from teatree.core.handover_orchestration import (
    _has_pending_work,
    _is_subagent_worktree,
    drive_subagents_to_fast_push,
    in_flight_subagent_worktrees,
)
from teatree.core.push.fast_push import FastPushOutcome
from teatree.utils.run import run_checked


@dataclass
class RecordingPusher:
    """A fake FastPusher: records the worktree it was built for, returns an OK outcome."""

    worktree: Path
    seen: list[Path]

    def run(self) -> FastPushOutcome:
        self.seen.append(self.worktree.resolve())
        return FastPushOutcome(ok=True, branch=self.worktree.name, committed=True, pushed=True)


@dataclass
class PusherFactoryRecorder:
    seen: list[Path] = field(default_factory=list)

    def __call__(self, worktree: Path) -> RecordingPusher:
        return RecordingPusher(worktree=worktree, seen=self.seen)


def _git(*args: str, cwd: Path) -> None:
    run_checked(["git", *args], cwd=cwd)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone with a pushed ``main`` and ``origin/HEAD`` set, ready for worktrees."""
    origin = tmp_path / "origin.git"
    run_checked(["git", "init", "--bare", str(origin)])
    work = tmp_path / "clone"
    run_checked(["git", "init", "-b", "main", str(work)])
    _git("config", "user.email", "agent@users.noreply.github.com", cwd=work)
    _git("config", "user.name", "agent", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    (work / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    _git("remote", "set-head", "origin", "main", cwd=work)
    return work


def _add_subagent_worktree(clone: Path, base: Path, name: str, branch: str) -> Path:
    path = base / ".claude" / "worktrees" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-b", branch, str(path), "main", cwd=clone)
    return path


class TestIsSubagentWorktree:
    def test_agent_dir_under_claude_worktrees_is_a_subagent(self, tmp_path: Path) -> None:
        assert _is_subagent_worktree(tmp_path / ".claude" / "worktrees" / "agent-abc")

    def test_non_agent_prefixed_dir_is_not(self, tmp_path: Path) -> None:
        assert not _is_subagent_worktree(tmp_path / ".claude" / "worktrees" / "ticket-123")

    def test_agent_dir_elsewhere_is_not(self, tmp_path: Path) -> None:
        assert not _is_subagent_worktree(tmp_path / "somewhere" / "agent-abc")

    def test_a_worktree_under_a_session_job_dir_is_a_subagent(self, tmp_path: Path) -> None:
        """The five stranded worktrees in the #4194 incident all lived here, unseen by the barrier."""
        assert _is_subagent_worktree(tmp_path / ".claude" / "jobs" / "0e077e62" / "tmp" / "wt4101")

    def test_a_job_dirs_direct_child_is_a_subagent(self, tmp_path: Path) -> None:
        assert _is_subagent_worktree(tmp_path / ".claude" / "jobs" / "0e077e62" / "wt")

    def test_the_job_dir_itself_is_not_a_worktree(self, tmp_path: Path) -> None:
        assert not _is_subagent_worktree(tmp_path / ".claude" / "jobs" / "0e077e62")

    def test_a_jobs_dir_outside_claude_is_not(self, tmp_path: Path) -> None:
        assert not _is_subagent_worktree(tmp_path / "elsewhere" / "jobs" / "0e077e62" / "tmp" / "wt")


class TestHasPendingWork:
    def test_dirty_tree_is_pending(self, clone: Path, tmp_path: Path) -> None:
        wt = _add_subagent_worktree(clone, tmp_path, "agent-dirty", "feat/dirty")
        (wt / "scratch.txt").write_text("uncommitted\n")
        assert _has_pending_work(wt)

    def test_clean_synced_worktree_is_not_pending(self, clone: Path, tmp_path: Path) -> None:
        wt = _add_subagent_worktree(clone, tmp_path, "agent-clean", "feat/clean")
        assert not _has_pending_work(wt)

    def test_unpushed_commit_beyond_default_is_pending(self, clone: Path, tmp_path: Path) -> None:
        wt = _add_subagent_worktree(clone, tmp_path, "agent-ahead", "feat/ahead")
        (wt / "new.txt").write_text("work\n")
        _git("add", "-A", cwd=wt)
        _git("commit", "-m", "unpushed work", cwd=wt)
        assert _has_pending_work(wt)

    def test_ahead_of_upstream_is_pending(self, clone: Path, tmp_path: Path) -> None:
        wt = _add_subagent_worktree(clone, tmp_path, "agent-up", "feat/up")
        _git("push", "-u", "origin", "feat/up", cwd=wt)
        (wt / "more.txt").write_text("more\n")
        _git("add", "-A", cwd=wt)
        _git("commit", "-m", "beyond upstream", cwd=wt)
        assert _has_pending_work(wt)


class TestDriveSubagents:
    def test_drives_only_pending_subagents_and_excludes_self(self, clone: Path, tmp_path: Path) -> None:
        dirty = _add_subagent_worktree(clone, tmp_path, "agent-dirty", "feat/dirty")
        (dirty / "scratch.txt").write_text("uncommitted\n")
        _add_subagent_worktree(clone, tmp_path, "agent-clean", "feat/clean")  # clean+synced → skipped
        # A dirty NON-sub-agent worktree must be left alone.
        non_agent = tmp_path / "other" / "ticket-1"
        non_agent.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-b", "feat/ticket", str(non_agent), "main", cwd=clone)
        (non_agent / "scratch.txt").write_text("dirty\n")

        recorder = PusherFactoryRecorder()
        pushes = drive_subagents_to_fast_push(str(clone), exclude=(clone,), pusher_factory=recorder)

        driven_paths = {p.worktree.resolve() for p in pushes}
        assert driven_paths == {dirty.resolve()}
        assert recorder.seen == [dirty.resolve()]
        assert all(p.driven for p in pushes)

    def test_excludes_the_orchestrators_own_worktree(self, clone: Path, tmp_path: Path) -> None:
        self_wt = _add_subagent_worktree(clone, tmp_path, "agent-self", "feat/self")
        (self_wt / "scratch.txt").write_text("uncommitted\n")

        recorder = PusherFactoryRecorder()
        pushes = drive_subagents_to_fast_push(str(clone), exclude=(self_wt,), pusher_factory=recorder)

        assert pushes == []
        assert recorder.seen == []

    def test_a_pusher_failure_is_recorded_not_raised(self, clone: Path, tmp_path: Path) -> None:
        dirty = _add_subagent_worktree(clone, tmp_path, "agent-boom", "feat/boom")
        (dirty / "scratch.txt").write_text("uncommitted\n")

        def _explode(_worktree: Path) -> RecordingPusher:
            msg = "push failed"
            raise RuntimeError(msg)

        pushes = drive_subagents_to_fast_push(str(clone), exclude=(clone,), pusher_factory=_explode)

        assert len(pushes) == 1
        assert pushes[0].driven is False
        assert "push failed" in pushes[0].error

    def test_enumeration_yields_pending_subagents(self, clone: Path, tmp_path: Path) -> None:
        dirty = _add_subagent_worktree(clone, tmp_path, "agent-e", "feat/e")
        (dirty / "scratch.txt").write_text("uncommitted\n")
        records = list(in_flight_subagent_worktrees(str(clone), exclude=(clone,)))
        assert [r.path.resolve() for r in records] == [dirty.resolve()]

    def test_a_worktree_named_in_exclude_is_skipped_even_when_the_cwd_is_below_it(
        self, clone: Path, tmp_path: Path
    ) -> None:
        """CHARACTERISATION, NOT a fix guard — green before the caller's fix and after it.

        ``exclude`` matches on the resolved worktree ROOT, so naming that root skips the
        worktree whatever subdirectory the caller happens to be sitting in, while naming
        only the subdirectory does not. The bug was the CALLER passing its cwd alone;
        this pins the contract the caller now relies on, and would have stayed green
        throughout — do not mistake it for the guard.
        """
        own = _add_subagent_worktree(clone, tmp_path, "agent-own", "feat/own")
        (own / "scratch.txt").write_text("uncommitted\n")
        subdirectory = own / "src" / "deep"
        subdirectory.mkdir(parents=True)

        by_root = list(in_flight_subagent_worktrees(str(clone), exclude=(clone, own)))
        by_subdirectory = list(in_flight_subagent_worktrees(str(clone), exclude=(clone, subdirectory)))

        assert by_root == []
        assert [record.path.resolve() for record in by_subdirectory] == [own.resolve()]
