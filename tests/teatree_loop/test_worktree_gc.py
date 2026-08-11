"""The pressure loop's heuristic worktree GC, driven through real git (#128, #4244).

The safety cases run the whole ladder against real worktrees under ``tmp_path``:
a dirty one, one ahead of its upstream, one this process is inside, one a foreign
process is inside, and one with the flag off — none may be removed. The others
pin what the GC exists for at all: enumeration from a root that is NOT itself a
repository, and a plan that says what it considered rather than falling silent
when it could not read anything.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import process_table
from teatree.core.cleanup.checkout_registry import CheckoutRegistry
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.models.resource_pressure_marker import ResourcePressureMarker
from teatree.loop import mechanical_resources, worktree_gc
from teatree.loop.mechanical_resources import free_resources

_GIT = shutil.which("git") or "/usr/bin/git"


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(
        [_GIT if args[0] == "git" else args[0], *args[1:]],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


class _GcFixture(TestCase):
    """A real origin, a real clone, and real worktrees pushed to it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rp_wt_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.enterContext(patch.object(mechanical_resources, "_run_uv_cache_prune", lambda: None))
        # The enumeration walks real directories, so it is pinned to this test's
        # own tree — the conftest guard's empty default would find no worktrees.
        self.enterContext(patch("teatree.core.cleanup.checkout_registry.checkout_scan_roots", return_value=(self.tmp,)))
        self.origin = self.tmp / "origin.git"
        self._seed_origin()

    def _seed_origin(self) -> None:
        seed = self.tmp / "_seed"
        seed.mkdir()
        _run("git", "init", "--initial-branch=main", str(seed), cwd=self.tmp)
        (seed / "a.txt").write_text("a")
        _run("git", "add", "a.txt", cwd=seed)
        _run("git", "commit", "-m", "first", cwd=seed)
        _run("git", "init", "--bare", "--initial-branch=main", str(self.origin), cwd=self.tmp)
        _run("git", "remote", "add", "origin", str(self.origin), cwd=seed)
        _run("git", "push", "-u", "origin", "main", cwd=seed)
        self.main_clone = self.tmp / "main_clone"
        _run("git", "clone", str(self.origin), str(self.main_clone), cwd=self.tmp)

    def _add_worktree(self, name: str, branch: str) -> Path:
        wt = self.tmp / name
        _run("git", "worktree", "add", "-b", branch, str(wt), "main", cwd=self.main_clone)
        _run("git", "push", "-u", "origin", branch, cwd=wt)
        return wt

    def _make_stale(self, wt: Path) -> None:
        old = 1_600_000_000  # well over 30 days ago
        os.utime(wt, (old, old))

    def _payload(self) -> dict:
        return {
            "resource": "disk",
            "disk_cache_allowlist": [],
            "allow_destructive_disk": True,
            "worktree_stale_days": 30,
            "max_worktree_gc_per_tick": 5,
        }


class WorktreeGcSafetyTests(_GcFixture):
    """Removes only clean + pushed + stale + nobody-inside worktrees."""

    def test_clean_pushed_stale_worktree_is_removed(self) -> None:
        """The control: without it every "must not be removed" case below is vacuous."""
        wt = self._add_worktree("clean", "feat-clean")
        self._make_stale(wt)
        with patch.object(worktree_gc, "worktree_root", return_value=self.main_clone):
            free_resources(self._payload())
        assert not wt.exists(), "a clean+pushed+stale worktree should be GC'd"

    def test_dirty_worktree_is_skipped(self) -> None:
        wt = self._add_worktree("dirty", "feat-dirty")
        (wt / "a.txt").write_text("locally modified")  # tracked-dirty
        self._make_stale(wt)
        with patch.object(worktree_gc, "worktree_root", return_value=self.main_clone):
            free_resources(self._payload())
        assert wt.exists(), "a dirty worktree must never be removed"

    def test_ahead_of_upstream_worktree_is_skipped(self) -> None:
        wt = self._add_worktree("ahead", "feat-ahead")
        (wt / "b.txt").write_text("new")
        _run("git", "add", "b.txt", cwd=wt)
        _run("git", "commit", "-m", "unpushed", cwd=wt)  # ahead of upstream, not pushed
        self._make_stale(wt)
        with patch.object(worktree_gc, "worktree_root", return_value=self.main_clone):
            free_resources(self._payload())
        assert wt.exists(), "an ahead-of-upstream worktree must never be removed"

    def test_active_session_cwd_worktree_is_never_removed(self) -> None:
        wt = self._add_worktree("active", "feat-active")
        self._make_stale(wt)
        with (
            patch.object(worktree_gc, "worktree_root", return_value=self.main_clone),
            patch.object(worktree_gc, "safe_cwd", return_value=wt.resolve()),
        ):
            free_resources(self._payload())
        assert wt.exists(), "the active-session worktree must never be GC'd"

    def test_a_live_process_inside_a_worktree_keeps_it(self) -> None:
        """The guard the heuristic lacked: clean+pushed+stale describes a busy worktree too."""
        wt = self._add_worktree("busy", "feat-busy")
        self._make_stale(wt)
        host_proc = self.tmp / "host-proc"
        (host_proc / "4242").mkdir(parents=True)
        (host_proc / "4242" / "cwd").symlink_to(wt / "src")
        with (
            patch.object(worktree_gc, "worktree_root", return_value=self.main_clone),
            patch.object(process_table, "_HOST_PROC_ROOT", host_proc),
        ):
            free_resources(self._payload())
        assert wt.exists(), "a worktree with a live process inside must never be GC'd"

    def test_gc_off_removes_nothing(self) -> None:
        wt = self._add_worktree("clean", "feat-clean")
        self._make_stale(wt)
        payload = self._payload()
        payload["allow_destructive_disk"] = False
        with patch.object(worktree_gc, "worktree_root", return_value=self.main_clone):
            free_resources(payload)
        assert wt.exists(), "with the flag off, NO worktree is removed"


class WorktreeGcReportingTests(_GcFixture):
    """What the pass says about itself — the half whose absence made the defect invisible."""

    def test_the_plan_reports_what_it_considered_and_kept(self) -> None:
        wt = self._add_worktree("fresh", "feat-fresh")  # not stale — kept
        with patch.object(worktree_gc, "worktree_root", return_value=self.main_clone):
            free_resources(self._payload())
        plan = ResourcePressureMarker.load().last_plan
        assert "GC worktrees: considered=1 eligible=0 kept=1" in plan
        assert str(wt) in plan

    def test_an_unreadable_registry_is_an_error_not_an_empty_consideration(self) -> None:
        """The defect's signature: an enumeration that could not run read as "nothing to do"."""
        self._add_worktree("clean", "feat-clean")
        (self.main_clone / ".git" / "HEAD").unlink()  # a real broken registry
        with patch.object(worktree_gc, "worktree_root", return_value=self.main_clone):
            free_resources(self._payload())
        assert "ERROR worktree enumeration incomplete" in ResourcePressureMarker.load().last_plan

    def test_the_done_worktree_sweep_runs_without_the_destructive_flag(self) -> None:
        """The one reclaim that demonstrably worked was reachable only by a human typing it."""
        payload = self._payload()
        payload["allow_destructive_disk"] = False
        with (
            patch.object(worktree_gc, "worktree_root", return_value=self.main_clone),
            patch("teatree.core.worktree.worktree_done.reap_done_worktrees", return_value=["reaped one"]) as mock_reap,
        ):
            free_resources(payload)
        mock_reap.assert_called_once()
        assert "done-worktree sweep handled 1 worktree row(s)" in ResourcePressureMarker.load().last_plan


class GcJudgementTests(TestCase):
    """Each judgement in isolation — all of them fail SAFE, to keeping the worktree."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.tmp = tmp_path

    def test_a_worktree_whose_directory_is_gone_is_kept_with_a_reason(self) -> None:
        reason = worktree_gc.keep_reason(
            self.tmp / "absent", stale_days=30, cwd=None, table=ProcessTable(frozenset(), "")
        )
        assert reason, "every non-candidate must carry a reason the plan can print"

    def test_git_declining_to_speak_reads_as_dirty(self) -> None:
        with patch.object(worktree_gc, "_git", return_value=None):
            assert worktree_gc.git_dirty(self.tmp) is True

    def test_git_declining_to_speak_reads_as_ahead(self) -> None:
        with patch.object(worktree_gc, "_git", return_value=None):
            assert worktree_gc.git_ahead_of_upstream(self.tmp) is True

    def test_an_unreadable_mtime_is_not_stale(self) -> None:
        with patch.object(worktree_gc.Path, "stat", side_effect=OSError):
            assert worktree_gc.is_stale(self.tmp, stale_days=30) is False

    def test_an_unresolvable_ancestor_contains_nothing(self) -> None:
        with patch.object(worktree_gc.Path, "resolve", side_effect=OSError):
            assert worktree_gc.is_within(self.tmp, self.tmp) is False

    def test_is_within_detects_nesting(self) -> None:
        child = self.tmp / "x" / "y"
        child.mkdir(parents=True)
        assert worktree_gc.is_within(child.resolve(), self.tmp) is True
        assert worktree_gc.is_within(self.tmp.resolve(), child) is False

    def test_an_unreadable_cwd_is_no_cwd(self) -> None:
        with patch.object(worktree_gc.Path, "cwd", side_effect=OSError):
            assert worktree_gc.safe_cwd() is None

    def test_git_without_a_binary_declines_to_speak(self) -> None:
        with patch.object(worktree_gc.shutil, "which", return_value=None):
            assert worktree_gc._git(self.tmp, "status") is None

    def test_the_survey_respects_the_per_tick_cap_and_says_what_it_deferred(self) -> None:
        worktrees = [self.tmp / f"wt{i}" for i in range(5)]
        for wt in worktrees:
            wt.mkdir()
        enumeration = CheckoutRegistry(frozenset(str(wt) for wt in worktrees), ())
        with (
            patch.object(worktree_gc, "linked_worktree_paths", return_value=enumeration),
            patch.object(worktree_gc, "safe_cwd", return_value=None),
            patch.object(worktree_gc, "keep_reason", return_value=""),
        ):
            survey = worktree_gc.survey_worktrees({"worktree_stale_days": 30, "max_worktree_gc_per_tick": 2})
        assert len(survey.candidates) == 2
        assert survey.considered == 5
        assert len(survey.kept) == 3, "the ones the cap deferred are reported, never silently dropped"

    def test_a_failed_removal_returns_no_bytes(self) -> None:
        survey = worktree_gc.GcSurvey(candidates=(str(self.tmp / "wt"),))
        with (
            patch.object(worktree_gc, "dir_size_gb", return_value=2.0),
            patch.object(worktree_gc, "remove_worktree", return_value=False),
        ):
            reclaimed = worktree_gc.collect(survey)
        assert reclaimed == pytest.approx(0.0), "a failed removal must not count toward reclaimed bytes"
