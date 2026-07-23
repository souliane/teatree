"""Broken worktree checkouts are reaped, not accumulated (#3583).

A dir whose ``.git`` pointer no longer resolves is invisible to every reaper
keyed on ``git worktree list``, so they piled up (16 on a real host) and emitted
an ``is not a git checkout`` WARN on every session setup. The reaper drops them;
the guards keep it from touching anything that could hold work.
"""

import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

import teatree.core.management.commands._workspace.broken_worktrees as broken_mod
from teatree.core.management.commands._workspace.broken_worktrees import (
    reap_broken_worktree_dirs,
    resolves_as_git_checkout,
)
from teatree.core.models import ConfigSetting, Ticket, Worktree
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

    def test_the_same_candidate_is_visited_once_across_repeated_roots(self) -> None:
        # A KEPT candidate (clean_ignore) survives the first root pass, so the same
        # root passed twice exercises the seen-set de-dup rather than re-deciding it.
        ConfigSetting.objects.set_value("clean_ignore", ["spike-*"])
        broken = _broken_checkout(self.tmp_path / "roots" / "spike-dupe")

        outcomes = reap_broken_worktree_dirs(self.tmp_path / "roots", self.tmp_path / "roots")

        assert broken.is_dir()
        # Decided exactly once despite appearing under two (identical) roots.
        assert len([line for line in outcomes if "spike-dupe" in line]) == 1

    def test_a_clean_ignore_match_is_kept(self) -> None:
        ConfigSetting.objects.set_value("clean_ignore", ["spike-*"])
        broken = _broken_checkout(self.tmp_path / "roots" / "spike-keepme")

        outcomes = reap_broken_worktree_dirs(self.tmp_path / "roots")

        assert broken.is_dir()
        assert any("matches clean_ignore" in line for line in outcomes)

    def test_a_removal_failure_is_reported_and_the_dir_is_kept(self) -> None:
        broken = _broken_checkout(self.tmp_path / "roots" / "wontgo")

        with mock.patch.object(broken_mod.shutil, "rmtree", side_effect=OSError("permission denied")):
            outcomes = reap_broken_worktree_dirs(self.tmp_path / "roots")

        assert broken.is_dir()
        assert any("removal failed" in line for line in outcomes)


class TestCheckoutProbe(_TmpPathTestCase):
    def test_probe_agrees_with_git(self) -> None:
        assert resolves_as_git_checkout(_real_checkout(self.tmp_path / "ok")) is True
        assert resolves_as_git_checkout(_broken_checkout(self.tmp_path / "bad")) is False
        assert resolves_as_git_checkout(self.tmp_path / "absent") is False
