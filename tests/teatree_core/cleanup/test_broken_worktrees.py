"""Broken worktree checkouts are reaped, not accumulated (#3583).

A dir whose ``.git`` pointer no longer resolves is invisible to every reaper
keyed on ``git worktree list``, so they piled up (16 on a real host) and emitted
an ``is not a git checkout`` WARN on every session setup. The reaper drops them;
the guards keep it from touching anything that could hold work.
"""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.management.commands._workspace.broken_worktrees import (
    reap_broken_worktree_dirs,
    resolves_as_git_checkout,
)
from teatree.core.models import Ticket, Worktree
from teatree.utils import git


def _real_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    git.run(repo=str(path), args=["init", "--quiet", "--initial-branch=main"])
    return path


def _broken_checkout(path: Path) -> Path:
    """A dir left behind when its source clone's worktree admin entry went away."""
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /nonexistent/clone/.git/worktrees/gone\n", encoding="utf-8")
    (path / "leftover.txt").write_text("x", encoding="utf-8")
    return path


class _TmpPathTestCase(TestCase):
    """A real on-disk scratch dir per test, torn down even when setUp fails."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)


class TestBrokenWorktreeReaper(_TmpPathTestCase):
    def test_a_dir_that_fails_rev_parse_is_dropped(self) -> None:
        broken = _broken_checkout(self.tmp_path / "roots" / "statusline-refresh")

        outcomes = reap_broken_worktree_dirs(self.tmp_path / "roots")

        assert not broken.exists()
        assert any("statusline-refresh" in line for line in outcomes)

    def test_a_healthy_checkout_is_never_touched(self) -> None:
        healthy = _real_checkout(self.tmp_path / "roots" / "live-work")

        outcomes = reap_broken_worktree_dirs(self.tmp_path / "roots")

        assert healthy.is_dir()
        assert outcomes == []

    def test_a_dir_with_no_git_entry_is_not_a_candidate(self) -> None:
        env_dir = self.tmp_path / "roots" / "a1b2c3d4e5f6"
        env_dir.mkdir(parents=True)
        (env_dir / "db.sqlite3").write_text("", encoding="utf-8")

        outcomes = reap_broken_worktree_dirs(self.tmp_path / "roots")

        assert env_dir.is_dir(), "an auto-isolated env dir is another reaper's business"
        assert outcomes == []

    def test_a_db_tracked_dir_is_left_to_its_row(self) -> None:
        broken = _broken_checkout(self.tmp_path / "roots" / "tracked")
        ticket = Ticket.objects.create(issue_url="https://example.invalid/org/repo/issues/1")
        Worktree.objects.create(
            ticket=ticket,
            overlay="",
            repo_path="org/repo",
            branch="tracked",
            extra={"worktree_path": str(broken)},
        )

        outcomes = reap_broken_worktree_dirs(self.tmp_path / "roots")

        assert broken.is_dir()
        assert any("its row owns the teardown" in line for line in outcomes)

    def test_several_roots_are_drained_in_one_pass(self) -> None:
        canonical = _broken_checkout(self.tmp_path / "canonical" / "one")
        alternate = _broken_checkout(self.tmp_path / "alternate" / "two")

        reap_broken_worktree_dirs(self.tmp_path / "canonical", self.tmp_path / "alternate")

        assert not canonical.exists()
        assert not alternate.exists()


class TestCheckoutProbe(_TmpPathTestCase):
    def test_probe_agrees_with_git(self) -> None:
        assert resolves_as_git_checkout(_real_checkout(self.tmp_path / "ok")) is True
        assert resolves_as_git_checkout(_broken_checkout(self.tmp_path / "bad")) is False
        assert resolves_as_git_checkout(self.tmp_path / "absent") is False
