"""#835 — dirty/unpushed worktree recovery capture before prune.

Split verbatim from the former monolithic ``tests/teatree_core/test_cleanup.py``
(souliane/teatree#443). These drive the production
``cleanup_worktree`` → ``capture_recovery_artifact`` seam against a real
bare-remote git topology under ``tmp_path``, plus the defensive branches of the
recovery helper; the shared ``GIT_*``-stripped runner is lifted into
``_shared``.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import CleanupResult, cleanup_worktree
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_recovery import _has_unpushed_commits, capture_recovery_artifact
from teatree.utils.run import CommandFailedError
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _recovery_dirs(temp_root: Path) -> list[Path]:
    return sorted(p for p in temp_root.iterdir() if p.is_dir() and p.name.startswith("t3-recover-"))


class TestCleanupWorktreeRecoversDirtyOrUnpushedWork(TestCase):
    """#835 — pruning a dirty/unpushed worktree captures recovery first.

    A worktree with uncommitted changes OR unpushed commits must first capture
    a self-contained, restorable recovery artifact (a git
    bundle of the branch + a working-tree diff) under the system temp dir, then
    remove the worktree. A clean, merged worktree must still hard-delete with no
    artifact written (preserve-behavior).
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.temp_root = tmp_path / "systmp"
        self.temp_root.mkdir()
        monkeypatch.setattr(
            "teatree.core.worktree_snapshot.tempfile.gettempdir",
            lambda: str(self.temp_root),
        )

        # A bare "remote" so origin/main exists and branch commits can be
        # classified as pushed-or-not.
        self.remote = tmp_path / "remote.git"
        _run_git("init", "-q", "--bare", "-b", "main", cwd=tmp_path)
        subprocess.run(
            [_GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )

        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        (self.repo_main / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self.branch = "ac-myrepo-835-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/835",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path)},
        )

    def _prune(self, worktree: Worktree) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=True)

    def _branch_exists(self) -> bool:
        branches = subprocess.run(
            [_GIT, "-C", str(self.repo_main), "branch", "--format=%(refname:short)"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.split()
        return self.branch in branches

    def test_uncommitted_changes_recovered_before_prune(self) -> None:
        # Uncommitted work in the worktree: an edit + a brand-new file.
        (self.wt_path / "base.txt").write_text("base\nDIRTY EDIT\n", encoding="utf-8")
        (self.wt_path / "newfile.txt").write_text("brand new\n", encoding="utf-8")

        self._prune(self._make_worktree())

        assert not self.wt_path.exists(), "worktree must still be removed"
        dirs = _recovery_dirs(self.temp_root)
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        rec = dirs[0]

        bundle = rec / "branch.bundle"
        diff = rec / "working-tree.diff"
        assert bundle.is_file(), "branch bundle missing"
        assert diff.is_file(), "working-tree diff missing"

        # The bundle is self-contained and verifiable. ``git bundle verify``
        # *requires* an ambient git repository (it checks the bundle's
        # prerequisites against it); run it inside the fixture's own repo via
        # ``-C`` rather than letting git discover one by walking up from the
        # process CWD. The latter is the Docker test-matrix trap: the suite runs
        # with CWD inside a worktree whose ``.git`` gitdir pointer is a host
        # path that does not exist in the container, so an unanchored verify
        # exits 128 ("not a git repository") on a perfectly valid bundle.
        subprocess.run(
            [_GIT, "-C", str(self.repo_main), "bundle", "verify", str(bundle)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )

        # Reconstruct: clone from the bundle, apply the saved diff — the dirty
        # edit and the untracked file must both come back. ``cwd`` is pinned to
        # the (non-repo) temp root so clone never discovers the broken ambient
        # worktree while resolving config/safe.directory.
        restore = self.temp_root / "restore-dirty"
        subprocess.run(
            [_GIT, "clone", "-q", "-b", self.branch, str(bundle), str(restore)],
            check=True,
            capture_output=True,
            cwd=str(self.temp_root),
            env=_clean_env(),
        )
        subprocess.run(
            [_GIT, "-C", str(restore), "apply", str(diff)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        assert (restore / "base.txt").read_text(encoding="utf-8") == "base\nDIRTY EDIT\n"
        assert (restore / "newfile.txt").read_text(encoding="utf-8") == "brand new\n"

    def test_unpushed_commits_recovered_before_prune(self) -> None:
        # A committed-but-never-pushed change set (clean working tree).
        (self.wt_path / "feature.txt").write_text("feature work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: unpushed feature", cwd=self.wt_path)

        self._prune(self._make_worktree())

        assert not self.wt_path.exists()
        dirs = _recovery_dirs(self.temp_root)
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        bundle = dirs[0] / "branch.bundle"
        assert bundle.is_file(), "branch bundle missing"
        # ``-C`` anchors verify to the fixture repo — see the explanatory
        # comment in test_uncommitted_changes_recovered_before_prune for why an
        # unanchored ``git bundle verify`` exits 128 in the Docker test matrix.
        subprocess.run(
            [_GIT, "-C", str(self.repo_main), "bundle", "verify", str(bundle)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        restore = self.temp_root / "restore-unpushed"
        subprocess.run(
            [_GIT, "clone", "-q", "-b", self.branch, str(bundle), str(restore)],
            check=True,
            capture_output=True,
            cwd=str(self.temp_root),
            env=_clean_env(),
        )
        log = subprocess.run(
            [_GIT, "-C", str(restore), "log", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert "feat: unpushed feature" in log
        assert (restore / "feature.txt").read_text(encoding="utf-8") == "feature work\n"

    def test_recovered_diff_applies_under_diff_noprefix_config(self) -> None:
        """#835 regression — a user with ``diff.noprefix=true`` must still recover.

        ``git diff`` honours the caller's git config. With ``diff.noprefix=true``
        set (common; was set on the review machine) the produced patch has no
        ``a/``/``b/`` prefixes and a plain ``git apply`` of it FAILS — the
        captured uncommitted work becomes unrestorable (the exact data-loss
        scenario #835 prevents). The capture must force standard prefixes so the
        restore contract holds regardless of user config. The repo-local scope
        is deliberate: do NOT rely on the conftest HOME sandbox, which masks
        real user git config.
        """
        # Repo-local config — survives the HOME sandbox the conftest installs.
        _run_git("config", "diff.noprefix", "true", cwd=self.repo_main)
        _run_git("config", "diff.noprefix", "true", cwd=self.wt_path)

        (self.wt_path / "base.txt").write_text("base\nDIRTY EDIT\n", encoding="utf-8")
        (self.wt_path / "newfile.txt").write_text("brand new\n", encoding="utf-8")

        self._prune(self._make_worktree())

        dirs = _recovery_dirs(self.temp_root)
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        bundle = dirs[0] / "branch.bundle"
        diff = dirs[0] / "working-tree.diff"
        assert bundle.is_file()
        assert diff.is_file()

        restore = self.temp_root / "restore-noprefix"
        subprocess.run(
            [_GIT, "clone", "-q", "-b", self.branch, str(bundle), str(restore)],
            check=True,
            capture_output=True,
            cwd=str(self.temp_root),
            env=_clean_env(),
        )
        # Plain ``git apply`` — the contract the docstring + BLUEPRINT promise.
        # RED on ``git diff HEAD --binary`` (no prefix flags); GREEN once the
        # capture forces ``--src-prefix=a/ --dst-prefix=b/``.
        subprocess.run(
            [_GIT, "-C", str(restore), "apply", str(diff)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        assert (restore / "base.txt").read_text(encoding="utf-8") == "base\nDIRTY EDIT\n"
        assert (restore / "newfile.txt").read_text(encoding="utf-8") == "brand new\n"

    def test_capture_failure_on_dirty_worktree_aborts_removal(self) -> None:
        """#1506 — capture failure on a DIRTY worktree must NOT destroy the work.

        Under ``force=True`` the data-loss guards are skipped, so the recovery
        artifact is the only protection. When that capture itself fails (disk
        full, bundle error) the prior behaviour fell through to ``worktree
        remove`` + ``branch -D``, destroying the very commits/edits the capture
        was meant to save. The corrected contract: re-check whether the worktree
        actually had work to lose and, if so, refuse the teardown (raise, like
        the non-force #706 guard) — leaving the worktree on disk, its branch,
        and its tracking DB row all intact.

        Drives the production seam (``cleanup_worktree`` → ``_remove_git_worktree``)
        with the real on-disk worktree; only the (unstoppable, deliberately
        failing) capture is patched to raise.
        """
        (self.wt_path / "base.txt").write_text("base\nDIRTY EDIT\n", encoding="utf-8")
        wt = self._make_worktree()
        boom = RuntimeError("disk full while bundling")

        with (
            patch("teatree.core.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.capture_recovery_artifact", side_effect=boom),
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            with pytest.raises(RuntimeError, match="refused teardown"):
                cleanup_worktree(wt, force=True)

        assert self.wt_path.exists(), "dirty worktree must NOT be removed when capture failed"
        assert self._branch_exists(), "branch must survive when capture failed on dirty work"
        assert Worktree.objects.filter(branch=self.branch).exists(), "DB row must survive (not orphaned on disk)"

    def test_capture_failure_on_clean_pushed_worktree_still_reaped(self) -> None:
        """#1506/#835 — capture failure on a CLEAN+PUSHED worktree still reaps.

        Capture is a no-op for a clean, fully-pushed worktree (nothing to lose),
        so a failure of that no-op must not block the prune — preserving #835's
        non-blocking-cleanup intent for the safe case. The worktree is removed
        and its branch deleted; the capture error is still surfaced.
        """
        _run_git("push", "-q", "origin", f"{self.branch}:main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        wt = self._make_worktree()
        boom = RuntimeError("disk full while bundling")

        with (
            patch("teatree.core.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.capture_recovery_artifact", side_effect=boom),
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            result = cleanup_worktree(wt, force=True)  # must NOT raise

        assert not self.wt_path.exists(), "clean+pushed worktree must still be reaped"
        assert result.clean is False
        assert any(f"recovery capture failed for {self.branch}" in e for e in result.errors)
        assert any("disk full while bundling" in e for e in result.errors)
        assert _recovery_dirs(self.temp_root) == [], "no artifact when capture itself failed"

    def test_capture_failure_on_clean_unpushed_worktree_aborts_removal(self) -> None:
        """#1506 — a clean working tree with UNPUSHED commits still aborts.

        The branch holds the only copy of the committed work; a clean working
        tree does not make it safe. Capture-failure must abort the destructive
        ``branch -D`` so the commits survive.
        """
        (self.wt_path / "feature.txt").write_text("feature work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: unpushed feature", cwd=self.wt_path)
        wt = self._make_worktree()
        boom = RuntimeError("disk full while bundling")

        with (
            patch("teatree.core.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.capture_recovery_artifact", side_effect=boom),
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            with pytest.raises(RuntimeError, match="refused teardown"):
                cleanup_worktree(wt, force=True)

        assert self._branch_exists(), "branch with unpushed commits must survive"
        assert Worktree.objects.filter(branch=self.branch).exists()

    def test_capture_failure_with_missing_dir_and_unpushed_commits_aborts(self) -> None:
        """#1506 — a gone worktree dir does NOT make unpushed branch commits safe.

        The unpushed-commit probe must run independently of the worktree dir's
        existence; the commits live in the main clone's object store. With the
        dir already removed but the branch unpushed, ``branch -D`` must still be
        aborted.
        """
        (self.wt_path / "feature.txt").write_text("feature work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: unpushed feature", cwd=self.wt_path)
        # Remove the worktree dir out-of-band (the capture-failure re-check then
        # sees a missing dir but a branch that is still unpushed).
        _run_git("worktree", "remove", "--force", str(self.wt_path), cwd=self.repo_main)
        assert not self.wt_path.exists()
        wt = self._make_worktree()
        boom = RuntimeError("disk full while bundling")

        with (
            patch("teatree.core.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.capture_recovery_artifact", side_effect=boom),
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            with pytest.raises(RuntimeError, match="refused teardown"):
                cleanup_worktree(wt, force=True)

        assert self._branch_exists(), "unpushed branch must survive a gone-dir capture failure"
        assert Worktree.objects.filter(branch=self.branch).exists()

    def test_capture_failure_with_inconclusive_status_aborts(self) -> None:
        """#1506 — an inconclusive ``git status`` must fail closed (might be dirty).

        Branch is pushed (no unpushed commits), but the strict dirty probe
        raises (lock contention / corrupt index). The re-check must treat the
        un-determinable working-tree state as "might have work to lose" and
        abort the destructive remove rather than assume clean.
        """
        _run_git("push", "-q", "origin", self.branch, cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        wt = self._make_worktree()
        boom = RuntimeError("disk full while bundling")

        with (
            patch("teatree.core.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.capture_recovery_artifact", side_effect=boom),
            patch(
                "teatree.core.cleanup.git.status_porcelain_strict",
                side_effect=CommandFailedError(["git"], 128, "", "index.lock"),
            ),
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            with pytest.raises(RuntimeError, match="refused teardown"):
                cleanup_worktree(wt, force=True)

        assert self.wt_path.exists(), "inconclusive status must abort the remove (fail closed)"
        assert self._branch_exists()
        assert Worktree.objects.filter(branch=self.branch).exists()

    def test_clean_merged_worktree_hard_deletes_with_no_artifact(self) -> None:
        # Branch tip == origin/main, clean working tree: nothing to lose.
        _run_git("push", "-q", "origin", f"{self.branch}:main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self._prune(self._make_worktree())

        assert not self.wt_path.exists(), "clean worktree must hard-delete"
        assert _recovery_dirs(self.temp_root) == [], "no recovery artifact for clean+merged worktree"


class TestWorktreeRecoveryEdgeCases(TestCase):
    """#835 — defensive branches of the recovery capture helper."""

    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def _worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/835",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch="ac-myrepo-835-x",
        )

    def test_returns_none_when_worktree_dir_absent(self) -> None:
        missing = "/nonexistent/worktree/path/that/does/not/exist"
        result = capture_recovery_artifact(Path("/nonexistent/repo"), missing, self._worktree())
        assert result is None

    def test_unpushed_probe_failure_fails_open_to_capture(self) -> None:
        """An inconclusive probe must be treated as "might have unpushed work"."""
        with patch(
            "teatree.core.worktree_snapshot.git.commits_absent_from_all_remotes",
            side_effect=CommandFailedError(["git"], 128, "", "corrupt"),
        ):
            assert _has_unpushed_commits(Path("/repo"), "some-branch") is True

    def test_bundle_failure_removes_partial_dir_and_reraises(self) -> None:
        """A failed capture must not leave a stray temp dir and must re-raise."""
        temp_root = self.tmp_path / "systmp"
        temp_root.mkdir()
        wt = self.tmp_path / "wt"
        wt.mkdir()
        self.monkeypatch.setattr("teatree.core.worktree_snapshot.tempfile.gettempdir", lambda: str(temp_root))
        with (
            patch("teatree.core.worktree_snapshot.git.status_porcelain", return_value=" M f"),
            patch("teatree.core.worktree_snapshot.git.commits_absent_from_all_remotes", return_value=[]),
            patch(
                "teatree.core.worktree_snapshot.git.bundle_create",
                side_effect=CommandFailedError(["git"], 128, "", "no repo"),
            ),
            pytest.raises(CommandFailedError),
        ):
            capture_recovery_artifact(self.tmp_path / "repo", str(wt), self._worktree())
        assert list(temp_root.iterdir()) == [], "partial recovery dir must be cleaned up"
