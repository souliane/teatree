"""Liveness guard — skip an actively-worked item (#2763), against real git + DB.

Anti-vacuous: a live Session, an active Task, a git index.lock, and a recent
HEAD commit each mark the worktree LIVE; a settled worktree (old commit, no
session/task, not the CWD) is NOT live.
"""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

import teatree.core.cleanup.cleanup_liveness as cl
from teatree.core.cleanup.cleanup_liveness import worktree_liveness
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import mark_external_delivery
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class _LivenessFixture(TestCase):
    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path: Path) -> None:
        self.wt_path = tmp_path / "wt"
        self.wt_path.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.wt_path)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        (self.wt_path / "f.txt").write_text("x\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        # Backdate the commit to a fixed UTC instant so the recency signal does not
        # fire by default and the windowed assertions below are deterministic.
        stamp = "2020-01-01T00:00:00 +0000"
        env = {**_clean_env(), "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}
        subprocess.run([_GIT, "-C", str(self.wt_path), "commit", "-q", "-m", "old"], check=True, env=env)
        self.commit_instant = datetime(2020, 1, 1, tzinfo=UTC)

    def _worktree(self) -> Worktree:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/1", state=Ticket.State.STARTED)
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="repo",
            branch="feature",
            extra={"worktree_path": str(self.wt_path)},
        )


class TestLiveSignals(_LivenessFixture):
    def test_settled_worktree_is_not_live(self) -> None:
        verdict = worktree_liveness(self._worktree(), wt_path=self.wt_path)
        assert verdict.active is False

    def test_live_session_marks_active(self) -> None:
        worktree = self._worktree()
        Session.objects.create(overlay="test", ticket=worktree.ticket)  # ended_at null = live
        verdict = worktree_liveness(worktree, wt_path=self.wt_path)
        assert verdict.active is True
        assert "session" in verdict.reason

    def test_claimed_task_marks_active(self) -> None:
        worktree = self._worktree()
        session = Session.objects.create(overlay="test", ticket=worktree.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=worktree.ticket, session=session, status=Task.Status.CLAIMED)
        verdict = worktree_liveness(worktree, wt_path=self.wt_path)
        assert verdict.active is True
        assert "task" in verdict.reason

    def test_completed_task_does_not_mark_active(self) -> None:
        worktree = self._worktree()
        session = Session.objects.create(overlay="test", ticket=worktree.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=worktree.ticket, session=session, status=Task.Status.COMPLETED)
        assert worktree_liveness(worktree, wt_path=self.wt_path).active is False

    def test_git_index_lock_marks_active(self) -> None:
        worktree = self._worktree()
        git_dir = subprocess.run(
            [_GIT, "-C", str(self.wt_path), "rev-parse", "--absolute-git-dir"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.strip()
        (Path(git_dir) / "index.lock").write_text("", encoding="utf-8")
        verdict = worktree_liveness(worktree, wt_path=self.wt_path)
        assert verdict.active is True
        assert "index.lock" in verdict.reason

    def test_recent_commit_marks_active(self) -> None:
        worktree = self._worktree()
        # `now` 3h after the commit, window 120m → commit is older than the cutoff → NOT recent.
        not_recent = self.commit_instant + timedelta(hours=3)
        assert worktree_liveness(worktree, wt_path=self.wt_path, now=not_recent, recent_minutes=120).active is False
        # `now` 30m after the commit, window 120m → within the window → recent → active.
        recent = self.commit_instant + timedelta(minutes=30)
        verdict = worktree_liveness(worktree, wt_path=self.wt_path, now=recent, recent_minutes=120)
        assert verdict.active is True
        assert "HEAD commit" in verdict.reason


class TestFsmTerminalBypass(_LivenessFixture):
    """The post-merge FSM teardown bypasses the two FSM-ceremony false positives (#2763).

    The merge transition itself mints the canonical phase session (busy-ticket) and
    writes the merge commit (recent-commit), so both fire spuriously the instant a
    ticket is done. ``fsm_terminal`` bypasses exactly those two; the genuine
    in-flight-operation guards (CWD, git index.lock) still fire.
    """

    def test_live_session_is_bypassed_on_fsm_terminal(self) -> None:
        worktree = self._worktree()
        Session.objects.create(overlay="test", ticket=worktree.ticket)  # the merge's own phase session
        assert worktree_liveness(worktree, wt_path=self.wt_path).active is True
        assert worktree_liveness(worktree, wt_path=self.wt_path, fsm_terminal=True).active is False

    def test_open_session_ignored_under_fsm_terminal(self) -> None:
        # A never-ended Session (ended_at NULL) pins a frozen ticket's worktree
        # ACTIVE forever on the ad-hoc path — the exact stale-session that kept
        # worktrees from being reaped. The post-terminal teardown passes
        # fsm_terminal=True so that stale session no longer blocks the purge,
        # while the ad-hoc sweep (fsm_terminal=False) still keeps it.
        worktree = self._worktree()
        session = Session.objects.create(overlay="test", ticket=worktree.ticket)
        assert session.ended_at is None
        assert worktree_liveness(worktree, wt_path=self.wt_path, fsm_terminal=False).active is True
        assert worktree_liveness(worktree, wt_path=self.wt_path, fsm_terminal=True).active is False

    def test_recent_commit_is_bypassed_on_fsm_terminal(self) -> None:
        worktree = self._worktree()
        recent = self.commit_instant + timedelta(minutes=30)  # within the 120m window
        assert worktree_liveness(worktree, wt_path=self.wt_path, now=recent, recent_minutes=120).active is True
        bypassed = worktree_liveness(worktree, wt_path=self.wt_path, now=recent, recent_minutes=120, fsm_terminal=True)
        assert bypassed.active is False

    def test_git_index_lock_still_fires_on_fsm_terminal(self) -> None:
        worktree = self._worktree()
        git_dir = subprocess.run(
            [_GIT, "-C", str(self.wt_path), "rev-parse", "--absolute-git-dir"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.strip()
        (Path(git_dir) / "index.lock").write_text("", encoding="utf-8")
        verdict = worktree_liveness(worktree, wt_path=self.wt_path, fsm_terminal=True)
        assert verdict.active is True
        assert "index.lock" in verdict.reason


class TestActiveDeliveryGuards(_LivenessFixture):
    """The #2227/#2773 active-delivery guards mark a worktree LIVE (#2763 reconciliation).

    Ported from #2773's shared liveness predicate into the FSM-done reaper's
    ``worktree_liveness`` so the ad-hoc ``clean-all`` sweep never reaps a worktree
    that is delivering externally / freshly e2e-tested / pinned. Unlike busy-ticket
    and recent-commit these are NOT FSM-ceremony false positives (the merge mints
    none of them), so they MUST still fire on the ``fsm_terminal`` post-merge path —
    the reconciled reaper protects MORE than #2773's ``respect_liveness=False``,
    never less.
    """

    def test_external_delivery_lease_marks_active(self) -> None:
        worktree = self._worktree()
        mark_external_delivery(worktree.ticket)
        verdict = worktree_liveness(worktree, wt_path=self.wt_path)
        assert verdict.active is True
        assert "external-delivery lease" in verdict.reason

    def test_external_delivery_lease_still_fires_on_fsm_terminal(self) -> None:
        worktree = self._worktree()
        mark_external_delivery(worktree.ticket)
        verdict = worktree_liveness(worktree, wt_path=self.wt_path, fsm_terminal=True)
        assert verdict.active is True, "an external-delivery lease must survive the post-merge teardown"
        assert "external-delivery lease" in verdict.reason

    def test_recent_e2e_run_marks_active(self) -> None:
        worktree = self._worktree()
        worktree.last_e2e_run = timezone.now()
        worktree.save(update_fields=["last_e2e_run"])
        verdict = worktree_liveness(worktree, wt_path=self.wt_path)
        assert verdict.active is True
        assert "E2E" in verdict.reason

    def test_recent_e2e_run_still_fires_on_fsm_terminal(self) -> None:
        worktree = self._worktree()
        worktree.last_e2e_run = timezone.now()
        worktree.save(update_fields=["last_e2e_run"])
        verdict = worktree_liveness(worktree, wt_path=self.wt_path, fsm_terminal=True)
        assert verdict.active is True, "a recent E2E run must survive the post-merge teardown"
        assert "E2E" in verdict.reason

    def test_old_e2e_run_does_not_mark_active(self) -> None:
        worktree = self._worktree()
        worktree.last_e2e_run = self.commit_instant  # 2020 — far outside the recency window
        worktree.save(update_fields=["last_e2e_run"])
        assert worktree_liveness(worktree, wt_path=self.wt_path).active is False

    def test_reaper_pinned_marks_active(self) -> None:
        worktree = self._worktree()
        worktree.extra = {**worktree.extra, "reaper_pinned": True}
        worktree.save(update_fields=["extra"])
        verdict = worktree_liveness(worktree, wt_path=self.wt_path)
        assert verdict.active is True
        assert "pinned" in verdict.reason

    def test_reaper_pinned_still_fires_on_fsm_terminal(self) -> None:
        worktree = self._worktree()
        worktree.extra = {**worktree.extra, "reaper_pinned": True}
        worktree.save(update_fields=["extra"])
        verdict = worktree_liveness(worktree, wt_path=self.wt_path, fsm_terminal=True)
        assert verdict.active is True, "an explicit reaper_pinned must survive the post-merge teardown"
        assert "pinned" in verdict.reason


class TestCwdScanSeesOtherProcesses(TestCase):
    """The CWD liveness signal sees ANY process working inside the worktree, not just the reaper's own.

    An ad-hoc agent (a shell, editor, dev server) with its CWD inside a worktree
    is live work. Checking only ``Path.cwd()`` — the reaper's own process — missed
    it; the signal now scans ``/proc/*/cwd`` too.
    """

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _fake_proc_with_cwd(self, pid: str, cwd_target: Path) -> Path:
        proc = self._tmp_path / "proc"
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cwd").symlink_to(cwd_target)
        (proc / "nonpid").mkdir()  # a non-numeric entry the scan must skip
        return proc

    def test_scans_proc_for_foreign_process_cwd_inside_worktree(self) -> None:
        wt = self._tmp_path / "wt"
        wt.mkdir()
        proc = self._fake_proc_with_cwd("1234", wt)
        with patch.object(cl, "_PROC_ROOT", proc):
            assert cl._any_process_cwd_within(wt.resolve()) is True

    def test_proc_scan_ignores_process_cwd_outside_worktree(self) -> None:
        wt = self._tmp_path / "wt"
        wt.mkdir()
        other = self._tmp_path / "other"
        other.mkdir()
        proc = self._fake_proc_with_cwd("1234", other)
        with patch.object(cl, "_PROC_ROOT", proc):
            assert cl._any_process_cwd_within(wt.resolve()) is False

    def test_worktree_liveness_marks_active_on_foreign_process_cwd(self) -> None:
        wt = self._tmp_path / "wt"
        wt.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/cwd", state=Ticket.State.STARTED)
        worktree = Worktree.objects.create(
            overlay="test", ticket=ticket, repo_path="repo", branch="feature", extra={"worktree_path": str(wt)}
        )
        with patch.object(cl, "_any_process_cwd_within", return_value=True):
            verdict = worktree_liveness(worktree, wt_path=wt)
        assert verdict.active is True
        assert "CWD" in verdict.reason
