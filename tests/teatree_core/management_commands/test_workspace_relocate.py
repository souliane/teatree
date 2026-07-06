# test-path: cross-cutting
"""Relocate teatree-managed worktrees to the per-overlay dir (real git).

Integration-first: a real ``git`` clone + worktree under ``tmp_path`` and real
``Worktree`` rows. Asserts ``git worktree move`` updates git's metadata AND the
DB row, that locked / dirty / active worktrees are SKIPPED, that the run is
idempotent and dry-run-safe, and that one failed move never aborts the rest.
"""

import subprocess
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from teatree.core.management.commands._workspace.relocate import RelocateIO, run_relocate
from teatree.core.models import Session, Ticket, Worktree
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _io() -> RelocateIO:
    return RelocateIO(write_out=lambda _m: None, write_err=lambda _m: None)


class _RelocateCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.old_ws = self._tmp() / "workspace"
        self.new_ws = self._tmp() / "workspace" / "t3-workspaces" / "test"
        self.clone = self.old_ws / "myrepo"
        self.clone.mkdir(parents=True)
        _run_git("init", "-q", "-b", "main", cwd=self.clone)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        _run_git("commit", "--allow-empty", "-q", "-m", "init", cwd=self.clone)
        self.branch = "12-fix-thing"
        self.old_wt = self.old_ws / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.old_wt), cwd=self.clone)

    def _tmp(self) -> Path:
        # Each TestCase method gets a fresh tmp via the addCleanup-managed dir.
        if not hasattr(self, "_tmpdir"):
            self._tmpdir = Path(tempfile.mkdtemp())
            self.addCleanup(
                lambda: subprocess.run(["/bin/rm", "-rf", str(self._tmpdir)], check=False, env=_clean_env())
            )
        return self._tmpdir

    def _make_row(self, *, issue: int = 12, wt_path: Path | None = None, clone_path: Path | None = None) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url=f"https://example.com/issues/{issue}")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra={
                "worktree_path": str(wt_path if wt_path is not None else self.old_wt),
                "clone_path": str(clone_path if clone_path is not None else self.clone),
            },
        )

    def _registered(self) -> str:
        return subprocess.run(
            [_GIT, "-C", str(self.clone), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout


class TestMove(_RelocateCase):
    def test_moves_worktree_updates_git_metadata_and_db_row(self) -> None:
        wt = self._make_row()
        target = self.new_ws / self.branch / "myrepo"

        result = run_relocate("test", self.new_ws, _io(), dry_run=False)

        assert result.moved
        assert not result.failed
        assert target.is_dir()
        assert not self.old_wt.exists()
        # git's worktree admin points at the NEW path (a raw mv would not do this).
        registered = self._registered()
        assert str(target.resolve()) in registered
        assert str(self.old_wt) not in registered
        wt.refresh_from_db()
        assert wt.worktree_path == str(target.resolve())

    def test_alias_spelled_overlay_still_matches(self) -> None:
        # The worktree.overlay "test" matches a t3-prefixed request canonically.
        self._make_row()
        result = run_relocate("t3-test", self.new_ws, _io(), dry_run=False)
        assert result.moved


class TestSkips(_RelocateCase):
    def test_skips_dirty_worktree(self) -> None:
        wt = self._make_row()
        (self.old_wt / "scratch.txt").write_text("uncommitted", encoding="utf-8")

        result = run_relocate("test", self.new_ws, _io(), dry_run=False)

        assert not result.moved
        assert any("uncommitted changes" in line for line in result.skipped)
        assert self.old_wt.exists()
        wt.refresh_from_db()
        assert wt.worktree_path == str(self.old_wt)

    def test_skips_locked_worktree(self) -> None:
        self._make_row()
        _run_git("worktree", "lock", str(self.old_wt), cwd=self.clone)

        result = run_relocate("test", self.new_ws, _io(), dry_run=False)

        assert not result.moved
        assert any("git-locked" in line for line in result.skipped)
        assert self.old_wt.exists()

    def test_skips_worktree_with_live_session(self) -> None:
        wt = self._make_row()
        Session.objects.create(overlay="test", ticket=wt.ticket)  # ended_at is null → live

        result = run_relocate("test", self.new_ws, _io(), dry_run=False)

        assert not result.moved
        assert any("live session" in line for line in result.skipped)
        assert self.old_wt.exists()

    def test_skips_when_cwd_inside_worktree(self) -> None:
        self._make_row()
        with patch(
            "teatree.core.management.commands._workspace.relocate._active_cwd",
            return_value=self.old_wt.resolve(),
        ):
            result = run_relocate("test", self.new_ws, _io(), dry_run=False)
        assert not result.moved
        assert any("active worktree" in line for line in result.skipped)


class TestIdempotentAndDryRun(_RelocateCase):
    def test_idempotent_when_already_under_target(self) -> None:
        # Point the target_root at the OLD workspace so the worktree is already under it.
        wt = self._make_row()
        result = run_relocate("test", self.old_ws, _io(), dry_run=False)
        assert not result.moved
        assert any("already under" in line for line in result.skipped)
        assert self.old_wt.exists()
        wt.refresh_from_db()
        assert wt.worktree_path == str(self.old_wt)

    def test_dry_run_plans_without_moving(self) -> None:
        wt = self._make_row()
        result = run_relocate("test", self.new_ws, _io(), dry_run=True)
        assert result.dry_run
        assert result.moved  # planned line present
        assert self.old_wt.exists()  # nothing moved
        assert not (self.new_ws / self.branch / "myrepo").exists()
        wt.refresh_from_db()
        assert wt.worktree_path == str(self.old_wt)


class TestContinuesPastFailure(_RelocateCase):
    def test_one_failed_move_does_not_abort_the_run(self) -> None:
        # A second repo+worktree that WILL move; the first has a bogus clone_path so
        # `git worktree move` fails — the run must report it and still move the second.
        good = self._make_row()
        bad_clone = self._tmp() / "not-a-repo"
        bad_clone.mkdir()
        bad = self._make_row(issue=98, clone_path=bad_clone)
        # Give the bad row its own on-disk worktree so it reaches the move step.
        bad_wt = self.old_ws / "98-other" / "myrepo"
        _run_git("worktree", "add", "-q", "-b", "98-other", str(bad_wt), cwd=self.clone)
        bad.extra = {**bad.extra, "worktree_path": str(bad_wt)}
        bad.save(update_fields=["extra"])

        result = run_relocate("test", self.new_ws, _io(), dry_run=False)

        assert len(result.moved) == 1
        assert len(result.failed) == 1
        good.refresh_from_db()
        assert good.worktree_path == str((self.new_ws / self.branch / "myrepo").resolve())
        bad.refresh_from_db()
        assert bad.worktree_path == str(bad_wt)  # unchanged after the failed move


class TestCliWiring(_RelocateCase):
    def test_relocate_command_resolves_overlay_and_target_and_moves(self) -> None:
        # The subcommand resolves the active overlay (T3_OVERLAY_NAME) and the
        # per-overlay target (T3_WORKSPACE_DIR back-compat override → new_ws).
        wt = self._make_row()
        target = self.new_ws / self.branch / "myrepo"
        with patch.dict("os.environ", {"T3_OVERLAY_NAME": "test", "T3_WORKSPACE_DIR": str(self.new_ws)}):
            out = cast("list[str]", call_command("workspace", "relocate"))
        assert any("moved" in line for line in out)
        assert target.is_dir()
        wt.refresh_from_db()
        assert wt.worktree_path == str(target.resolve())

    def test_relocate_command_dry_run_touches_nothing(self) -> None:
        self._make_row()
        with patch.dict("os.environ", {"T3_OVERLAY_NAME": "test", "T3_WORKSPACE_DIR": str(self.new_ws)}):
            out = cast("list[str]", call_command("workspace", "relocate", dry_run=True))
        assert any("would move" in line for line in out)
        assert self.old_wt.exists()


class TestHalfMoveReconcile(_RelocateCase):
    """A git move that succeeds but whose DB-row save throws is self-healed (#regroup).

    The reviewer's gap: ``_move_one`` did ``git.worktree_move`` then ``save`` — if
    save threw AFTER the git move, disk/git pointed at ``target`` but the row still
    said ``old``, and the next run skipped it forever as a stale row. The fix reports
    the half-move instead of aborting, and a subsequent run reconciles the row.
    """

    def test_git_move_succeeds_but_row_save_fails_then_reconciles(self) -> None:
        wt = self._make_row()
        target = self.new_ws / self.branch / "myrepo"

        # Make the post-move ``save(update_fields=["extra"])`` throw, leaving git +
        # disk at ``target`` while the row still records the OLD (now-gone) path.
        real_save = Worktree.save

        def fail_extra_save(self_wt: Worktree, *args: object, **kwargs: object) -> None:
            if kwargs.get("update_fields") == ["extra"]:
                msg = "simulated DB failure mid-relocate"
                raise OperationalError(msg)
            real_save(self_wt, *args, **kwargs)

        with patch.object(Worktree, "save", fail_extra_save):
            first = run_relocate("test", self.new_ws, _io(), dry_run=False)

        assert target.is_dir()  # the git move happened
        assert not self.old_wt.exists()
        assert first.failed  # reported, never silently lost
        assert not first.moved
        wt.refresh_from_db()
        assert wt.worktree_path == str(self.old_wt)  # row NOT updated — stale

        # A subsequent run RECONCILES the stale row to the already-moved location
        # (anti-vacuity: without the reconcile step it is skipped forever).
        out_lines: list[str] = []
        capturing_io = RelocateIO(write_out=out_lines.append, write_err=lambda _m: None)
        second = run_relocate("test", self.new_ws, capturing_io, dry_run=False)
        assert second.moved  # healed (a positive outcome), not skipped as a stale row
        assert not second.skipped
        assert any("reconcil" in line.lower() for line in out_lines)
        wt.refresh_from_db()
        assert Path(wt.worktree_path) == target.resolve()

    def test_dry_run_reports_half_move_without_writing(self) -> None:
        # Manufacture the half-move state directly (git move done, row left stale),
        # then a --dry-run run plans the reconcile but writes nothing.
        wt = self._make_row()
        target = self.new_ws / self.branch / "myrepo"
        target.parent.mkdir(parents=True, exist_ok=True)
        _run_git("worktree", "move", str(self.old_wt), str(target), cwd=self.clone)
        # The row still records the OLD path (the half-move).
        assert wt.worktree_path == str(self.old_wt)

        result = run_relocate("test", self.new_ws, _io(), dry_run=True)

        assert result.moved  # the reconcile is PLANNED (not skipped as a stale row)
        assert not result.skipped
        wt.refresh_from_db()
        assert wt.worktree_path == str(self.old_wt)  # nothing written under dry-run
