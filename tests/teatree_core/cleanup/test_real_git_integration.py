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
from django.test import TestCase

from teatree.core.cleanup.cleanup import CleanupResult, cleanup_worktree
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.cleanup._shared import _GIT, _RM, _clean_env, _run_git


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
        self.branch = "ac-myrepo-99-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _make_worktree(self, *, with_extras: bool) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/99",
            state=Ticket.State.IN_REVIEW,
        )
        extras = {"worktree_path": str(self.wt_path)} if with_extras else {}
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra=extras,
        )

    def _cleanup(self, worktree: Worktree) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=True)

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

    def test_removes_worktree_when_extras_missing_path(self) -> None:
        """#460 — without ``worktree_path`` in extras the dir + registry entry must still be removed."""
        wt = self._make_worktree(with_extras=False)
        self._cleanup(wt)
        assert not self.wt_path.exists(), "worktree directory survived cleanup"
        assert str(self.wt_path) not in self._registered_worktrees(), "git worktree registry entry survived"

    def test_surfaces_failure_in_errors_when_git_remove_fails(self) -> None:
        """When the git ops can't complete (source repo missing), the failure surfaces in ``errors`` (#877)."""
        wt = self._make_worktree(with_extras=True)
        # Wipe the source repo so git operations fail
        subprocess.run([_RM, "-rf", str(self.repo_main)], check=True, env=_clean_env())
        result = self._cleanup(wt)
        # The missing-source-repo failure is surfaced, not swallowed
        assert result.clean is False
        assert result.errors
        assert any("source repo missing" in e for e in result.errors)
        assert "with errors" in str(result)
        # Worktree row deleted regardless so the operator can retry without DB cruft
        assert not Worktree.objects.filter(pk=wt.pk).exists()


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
        self.branch = "ac-teatree-491-x"
        self.wt_path = self.workspace / self.branch / "teatree"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def test_resolves_namespaced_clone_via_extra(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/491",
            state=Ticket.State.IN_REVIEW,
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
            mock_overlay.return_value.get_cleanup_steps.return_value = []
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
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=True)

    def _worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/1462",
            state=Ticket.State.IN_REVIEW,
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
