"""Real-git integration for the ephemeral cold-review worktree reaper (#4576).

``add_review_worktree_at_head`` mkdtemps a ``t3-review-*`` detached checkout and
delegates removal to a caller that is a SEPARATE process, so a reviewer that dies
leaves the registration behind. The OS then reaps the directory and the
registration survives it: ``prune`` refuses clone-wide because the temp root lies
outside the venue's provisioning root, and a bare ``git worktree lock`` — which
that very refusal advises — makes it permanent.

These tests drive the sweep against real ``git worktree`` registrations under
``tmp_path`` and pin BOTH directions: an absent registration is deregistered even
when locked, and a live one with a real directory is untouched — the harmful
failure, since a lock is exactly what a deliberate operator claim looks like.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from teatree.core.worktree.review_worktree_reaper import REVIEW_REGISTRATION_TTL, reap_stale_review_worktrees
from teatree.core.worktree.venue_safe_registry import prune_refusal
from teatree.utils.review_checkout import REVIEW_WORKTREE_PREFIX
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class _ReviewRegistryFixture:
    """A clone plus a temp root standing in for the system temp dir."""

    @pytest.fixture(autouse=True)
    def _tmp_clone(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "clone"
        self.repo.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo)
        _run_git("config", "user.email", "t@t", cwd=self.repo)
        _run_git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "README").write_text("x")
        _run_git("add", "-A", cwd=self.repo)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo)
        self.temp_root = tmp_path / "vartmp"
        self.temp_root.mkdir()

    def _add_review_worktree(self, suffix: str, *, branch: str = "") -> Path:
        path = self.temp_root / f"{REVIEW_WORKTREE_PREFIX}{suffix}"
        args = ["worktree", "add", "-q", str(path)]
        args += ["-b", branch] if branch else ["--detach", "HEAD"]
        if branch:
            args.append("HEAD")
        _run_git(*args, cwd=self.repo)
        return path

    def _registered(self) -> str:
        return subprocess.run(
            [_GIT, "-C", str(self.repo), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout

    def _admin_gitdir(self, path: Path) -> Path:
        """The ``<common>/worktrees/<id>/gitdir`` file backing *path*'s registration."""
        for entry in (self.repo / ".git" / "worktrees").iterdir():
            gitdir = entry / "gitdir"
            if Path(gitdir.read_text().strip()).parent == path:
                return gitdir
        unregistered = f"no admin dir registers {path}"
        raise AssertionError(unregistered)

    def _age(self, path: Path, *, seconds: float) -> None:
        stamp = time.time() - seconds
        os.utime(self._admin_gitdir(path), (stamp, stamp))

    def _abandon(self, path: Path, *, seconds: float = REVIEW_REGISTRATION_TTL.total_seconds() * 2) -> Path:
        """Delete *path*'s directory and backdate its registration past the floor."""
        self._age(path, seconds=seconds)
        shutil.rmtree(path)
        return path

    def _reap(self, *, dry_run: bool = False) -> list[str]:
        return reap_stale_review_worktrees(str(self.repo), dry_run=dry_run)


class TestAbsentRegistrationsAreCleared(_ReviewRegistryFixture):
    def test_absent_unlocked_registration_is_deregistered(self) -> None:
        path = self._abandon(self._add_review_worktree("aaaaaaaa"))
        reports = self._reap()
        assert str(path) not in self._registered()
        assert any(str(path) in line for line in reports)

    def test_absent_reason_less_lock_is_deregistered(self) -> None:
        """The ticket's nine: prune skips a lock, and `remove --force` refuses one."""
        path = self._add_review_worktree("bbbbbbbb")
        self._abandon(path)
        _run_git("worktree", "lock", str(path), cwd=self.repo)
        self._reap()
        assert str(path) not in self._registered()

    def test_a_second_sweep_is_a_no_op(self) -> None:
        path = self._abandon(self._add_review_worktree("cccccccc"))
        self._reap()
        assert self._reap() == []
        assert str(path) not in self._registered()

    def test_dry_run_reports_without_deregistering(self) -> None:
        path = self._abandon(self._add_review_worktree("dddddddd"))
        reports = self._reap(dry_run=True)
        assert any("WOULD" in line and str(path) in line for line in reports)
        assert str(path) in self._registered()


class TestLiveRegistrationsSurvive(_ReviewRegistryFixture):
    def test_present_locked_review_worktree_is_untouched(self) -> None:
        """The harmful failure: a lock is what a deliberate claim looks like."""
        path = self._add_review_worktree("eeeeeeee")
        _run_git("worktree", "lock", str(path), cwd=self.repo)
        self._age(path, seconds=REVIEW_REGISTRATION_TTL.total_seconds() * 2)
        self._reap()
        registry = self._registered()
        assert str(path) in registry
        assert "locked" in registry
        assert path.is_dir()

    def test_present_unlocked_review_worktree_is_untouched(self) -> None:
        path = self._add_review_worktree("ffffffff")
        self._age(path, seconds=REVIEW_REGISTRATION_TTL.total_seconds() * 2)
        assert self._reap() == []
        assert str(path) in self._registered()

    def test_absent_lock_with_a_reason_is_kept(self) -> None:
        path = self._add_review_worktree("gggggggg")
        self._abandon(path)
        _run_git("worktree", "lock", "--reason", "in-flight PR for #4387", str(path), cwd=self.repo)
        reports = self._reap()
        assert str(path) in self._registered()
        assert any("in-flight PR for #4387" in line for line in reports)

    def test_registration_younger_than_the_floor_is_kept(self) -> None:
        path = self._add_review_worktree("hhhhhhhh")
        self._abandon(path, seconds=1)
        assert str(path) in self._registered()
        self._reap()
        assert str(path) in self._registered()

    def test_branch_holding_registration_is_never_reaped(self) -> None:
        path = self._add_review_worktree("iiiiiiii", branch="somebranch")
        self._abandon(path)
        self._reap()
        assert str(path) in self._registered()

    def test_non_review_registration_is_untouched(self) -> None:
        path = self.temp_root / "some-other-checkout"
        _run_git("worktree", "add", "-q", "--detach", str(path), "HEAD", cwd=self.repo)
        self._age(path, seconds=REVIEW_REGISTRATION_TTL.total_seconds() * 2)
        shutil.rmtree(path)
        assert self._reap() == []
        assert str(path) in self._registered()

    def test_unobservable_neighbourhood_is_never_reaped(self, tmp_path: Path) -> None:
        """An unreadable parent is missing evidence, never proof the checkout died."""
        path = self._add_review_worktree("jjjjjjjj")
        self._age(path, seconds=REVIEW_REGISTRATION_TTL.total_seconds() * 2)
        shutil.rmtree(self.temp_root)
        reports = self._reap()
        assert str(path) in self._registered()
        assert any("unreachable" in line for line in reports)


class TestPruneIsUnblocked(_ReviewRegistryFixture):
    def test_clone_wide_prune_refusal_clears_once_the_sweep_runs(self) -> None:
        """The leak withholds the WHOLE clone's prune, taking real dead rows with it."""
        path = self._abandon(self._add_review_worktree("kkkkkkkk"))
        assert str(path) in prune_refusal(str(self.repo))
        self._reap()
        assert prune_refusal(str(self.repo)) == ""
