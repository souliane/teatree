"""Real-git integration for on-disk worktree removal.

Split verbatim from the former monolithic ``tests/teatree_core/test_cleanup.py``
(souliane/teatree#443). These exercise ``cleanup_worktree`` against a real
``git worktree`` under ``tmp_path`` (#460 canonical-layout resolution and the
namespaced-clone case); the shared ``GIT_*``-stripped runner is lifted into
``_shared``.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.cleanup.cleanup import CleanupResult, cleanup_worktree
from teatree.core.cleanup.unshipped_work import bundle_path
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.models import Ticket, UnshippedWorkRecord, Worktree
from teatree.core.runners.worktree_teardown import WorktreeTeardownRunner
from tests.teatree_core.cleanup._shared import _GIT, _RM, _clean_env, _run_git


def _publish_base(repo_main: Path, origin: Path) -> None:
    origin.mkdir()
    _run_git("init", "-q", "--bare", "-b", "main", cwd=origin)
    _run_git("remote", "add", "origin", str(origin), cwd=repo_main)
    _run_git("push", "-q", "-u", "origin", "main", cwd=repo_main)


class TestCleanupWorktreeRemovesOnDiskWorktree(TestCase):
    """Real-git integration: cleanup must remove the on-disk worktree even when extras lack ``worktree_path``.

    Reproduces #460 — ``Worktree.extra['worktree_path']`` can be missing when
    a row exists without successful provisioning recording the path. The
    canonical layout (``workspace/<branch>/<repo-leaf>``) is enough to find
    and remove the on-disk worktree.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo_main)
        _publish_base(self.repo_main, tmp_path / "origin.git")
        self.branch = "ac-myrepo-99-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _make_worktree(self, *, with_extras: bool) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/99",
            state=Ticket.State.REVIEW_REQUESTED,
        )
        extras = {"worktree_path": str(self.wt_path)} if with_extras else {}
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra=extras,
        )

    def _cleanup(self, worktree: Worktree, *, discard_unshipped_work: bool = False) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=True, discard_unshipped_work=discard_unshipped_work)

    def _registered_worktrees(self) -> str:
        return subprocess.run(
            [_GIT, "-C", str(self.repo_main), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout

    def test_removes_worktree_when_extras_have_path(self) -> None:
        """Baseline — the existing happy path also exercises real git."""
        wt = self._make_worktree(with_extras=True)
        self._cleanup(wt)
        assert not self.wt_path.exists()
        assert str(self.wt_path) not in self._registered_worktrees()

    def test_force_keeps_ignored_file_and_worktree(self) -> None:
        wt = self._make_worktree(with_extras=True)
        (self.wt_path / ".gitignore").write_text("notes.local\n", encoding="utf-8")
        _run_git("add", ".gitignore", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "ignore notes", cwd=self.wt_path)
        (self.wt_path / "notes.local").write_text("keep\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match=r"notes\.local"):
            self._cleanup(wt)

        assert (self.wt_path / "notes.local").read_text() == "keep\n"
        assert str(self.wt_path) in self._registered_worktrees()

    def test_force_keeps_commit_that_no_remote_holds(self) -> None:
        wt = self._make_worktree(with_extras=True)
        (self.wt_path / "feature.py").write_text("value = 1\n", encoding="utf-8")
        _run_git("add", "feature.py", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "local feature", cwd=self.wt_path)

        with pytest.raises(RuntimeError, match=r"commit.*no live remote"):
            self._cleanup(wt)

        assert self.wt_path.is_dir()
        assert str(self.wt_path) in self._registered_worktrees()

    def test_discard_unshipped_work_removes_what_force_alone_keeps(self) -> None:
        wt = self._make_worktree(with_extras=True)
        (self.wt_path / "feature.py").write_text("value = 1\n", encoding="utf-8")
        _run_git("add", "feature.py", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "local feature", cwd=self.wt_path)
        (self.wt_path / "notes.txt").write_text("scratch\n", encoding="utf-8")

        self._cleanup(wt, discard_unshipped_work=True)

        assert not self.wt_path.exists()
        assert str(self.wt_path) not in self._registered_worktrees()
        assert not Worktree.objects.filter(pk=wt.pk).exists()

    def test_explicit_force_teardown_discards_but_an_unforced_one_keeps(self) -> None:
        wt = self._make_worktree(with_extras=True)
        (self.wt_path / "notes.txt").write_text("scratch\n", encoding="utf-8")

        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            unforced = WorktreeTeardownRunner(wt).run()
            assert not unforced.ok
            assert (self.wt_path / "notes.txt").is_file()
            forced = WorktreeTeardownRunner(wt, force=True).run()

        assert forced.ok
        assert not self.wt_path.exists()

    def test_unforced_runner_keeps_pushed_checkout_with_open_merge_request(self) -> None:
        wt = self._make_worktree(with_extras=True)
        _run_git("push", "-q", "origin", self.branch, cwd=self.wt_path)

        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch(
                "teatree.core.cleanup.removal_proof.find_open_pr_for_branch",
                return_value=PrProbe.found("https://forge.example/pull/7"),
            ),
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            result = WorktreeTeardownRunner(wt).run()

        assert not result.ok
        assert "open merge request" in result.detail
        assert self.wt_path.is_dir()
        assert Worktree.objects.filter(pk=wt.pk).exists()

    def test_removes_worktree_when_extras_missing_path(self) -> None:
        """#460 — without ``worktree_path`` in extras the dir + registry entry must still be removed."""
        wt = self._make_worktree(with_extras=False)
        self._cleanup(wt)
        assert not self.wt_path.exists(), "worktree directory survived cleanup"
        assert str(self.wt_path) not in self._registered_worktrees(), "git worktree registry entry survived"

    def test_surfaces_failure_in_errors_when_git_remove_fails(self) -> None:
        """A failed git removal is collected in ``errors`` (#877)."""
        wt = self._make_worktree(with_extras=True)
        with patch("teatree.core.cleanup.cleanup.git.worktree_remove", return_value=False):
            result = self._cleanup(wt)
        assert result.clean is False
        assert result.errors
        assert any("git worktree remove failed" in e for e in result.errors)
        assert "with errors" in str(result)
        assert not Worktree.objects.filter(pk=wt.pk).exists()

    def test_missing_source_repo_keeps_checkout(self) -> None:
        wt = self._make_worktree(with_extras=True)
        subprocess.run([_RM, "-rf", str(self.repo_main)], check=True, env=_clean_env())

        with pytest.raises(RuntimeError):
            self._cleanup(wt)

        assert self.wt_path.exists()
        assert Worktree.objects.filter(pk=wt.pk).exists()


class TestCleanupWorktreeNamespacedClone(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.repo_main = self.workspace / "souliane" / "teatree"
        self.repo_main.mkdir(parents=True)
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo_main)
        _publish_base(self.repo_main, tmp_path / "origin.git")
        self.branch = "ac-teatree-491-x"
        self.wt_path = self.workspace / self.branch / "teatree"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def test_resolves_namespaced_clone_via_extra(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/491",
            state=Ticket.State.REVIEW_REQUESTED,
        )
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="teatree",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path), "clone_path": str(self.repo_main)},
        )

        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            result = cleanup_worktree(wt, force=True)

        assert not self.wt_path.exists()
        registry = subprocess.run(
            [_GIT, "-C", str(self.repo_main), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert str(self.wt_path) not in registry
        assert result.clean is True
        assert result.errors == []


class TestCleanupCapturesUnshippedWorkBeforeRefusing(TestCase):
    """Forced teardown records unshipped work and keeps its checkout."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.captures = tmp_path / "captures"
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.repo_main)
        (self.repo_main / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "-u", "origin", "main", cwd=self.repo_main)
        self.branch = "ac-myrepo-706-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _cleanup_forced(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/706", state=Ticket.State.REVIEW_REQUESTED)
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path)},
        )
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.unshipped_work.get_data_dir", return_value=self.captures),
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            cleanup_worktree(wt, force=True)

    def test_staged_only_worktree_is_captured_before_force_refusal(self) -> None:
        (self.wt_path / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        _run_git("add", "tracked.py", cwd=self.wt_path)

        with pytest.raises(RuntimeError, match=r"tracked\.py"):
            self._cleanup_forced()

        assert self.wt_path.exists()
        record = UnshippedWorkRecord.objects.get(checkout_path=str(self.wt_path))
        assert record.branch == self.branch
        assert record.dirty_paths == ["tracked.py"]
        patch_text = bundle_path(record.artifact_prefix, ".uncommitted.patch").read_text(encoding="utf-8")
        assert "value = 2" in patch_text

    def test_synced_worktree_leaves_no_record(self) -> None:
        self._cleanup_forced()

        assert not UnshippedWorkRecord.objects.exists()

    def test_capture_db_failure_still_refuses_unshipped_work(self) -> None:
        (self.wt_path / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        _run_git("add", "tracked.py", cwd=self.wt_path)

        with (
            patch.object(
                UnshippedWorkRecord.objects,
                "update_or_create",
                side_effect=OperationalError("no such table: teatree_unshippedworkrecord"),
            ),
            pytest.raises(RuntimeError, match=r"tracked\.py"),
        ):
            self._cleanup_forced()

        assert self.wt_path.exists()


_PREK_HOOK = """#!/bin/sh
# File generated by prek: https://github.com/j178/prek

HERE="$(cd "$(dirname "$0")" && pwd)"
PREK="{prek_path}"

exec "$PREK" hook-impl --hook-dir "$HERE" --hook-type=pre-push -- "$@"
"""


class TestCleanupReapsStalePrekHook(TestCase):
    """souliane/teatree#1462 — teardown must drop a SHARED hook baked into the removed worktree.

    The pre-push hook lives in the SHARED ``.git/hooks`` (git common dir). A
    hook baked with an absolute ``PREK=`` path inside the worktree being torn
    down would hang every sibling worktree's ``git push``. Cleanup must remove
    that stale hook so the other worktrees keep running prek to completion.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo_main)
        _publish_base(self.repo_main, tmp_path / "origin.git")
        self.branch = "ac-myrepo-1462-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)
        self.hook = self.repo_main / ".git" / "hooks" / "pre-push"
        self.hook.parent.mkdir(parents=True, exist_ok=True)

    def _cleanup(self, worktree: Worktree) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=True)

    def _worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/1462",
            state=Ticket.State.REVIEW_REQUESTED,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path)},
        )

    def test_removes_hook_baked_into_the_torn_down_worktree(self) -> None:
        self.hook.write_text(_PREK_HOOK.format(prek_path=str(self.wt_path / ".venv" / "bin" / "prek")))
        self._cleanup(self._worktree())
        assert not self.hook.exists(), "stale prek hook pointing into the removed worktree survived teardown"

    def test_keeps_path_resolved_hook_so_other_worktrees_still_run_prek(self) -> None:
        self.hook.write_text(_PREK_HOOK.format(prek_path="prek"))
        self._cleanup(self._worktree())
        assert self.hook.exists(), "a PATH-resolved hook must survive teardown for the sibling worktrees"
