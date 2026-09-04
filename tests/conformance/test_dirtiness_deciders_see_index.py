"""Every checkout-dirtiness decider must call a STAGED-ONLY checkout dirty.

The behavioural half of the index-blindness guard (the mechanical half is
``tests/conformance/test_index_blind_git_diff.py``, which cannot see an argv
assembled across statements). Each entry below is a production decider whose
answer gates a keep / reap / clobber, driven against one real staged-only
checkout — invisible to a bare ``git diff``, and every one of them must still
report it as holding work.

A forward ratchet, not a repair: every decider listed already sees the index, so
this pins a property the tree has rather than fixing one it lacked. What it buys
is that a decider added to the set, or an existing one edited back onto the
working tree alone, goes red here.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.cli.update import _tracked_dirty_paths as update_tracked_dirty_paths
from teatree.core.cleanup.cleanup import _EffectiveTarget
from teatree.core.cleanup.orphan_checkouts import orphan_is_dirty
from teatree.core.cleanup.unshipped_work import probe_unshipped_work
from teatree.core.cleanup.working_tree_dirt import real_uncommitted_reasons
from teatree.core.handover_orchestration import _has_pending_work
from teatree.core.management.commands._workspace.cleanup import _worktree_clean
from teatree.core.models import Ticket, Worktree
from teatree.core.models.ticket_worktree_checks import worktree_tracked_dirty_path
from teatree.loop.scanners.pull_main_clone import _tracked_dirty_paths as pull_tracked_dirty_paths
from teatree.loop.scanners.self_update import _tracked_dirty_paths as self_update_tracked_dirty_paths
from teatree.loop.worktree_gc import git_dirty
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class TestEveryDirtinessDeciderSeesTheIndex(TestCase):
    @pytest.fixture(autouse=True)
    def _staged_only_checkout(self, tmp_path: Path) -> None:
        self.clone = tmp_path / "clone"
        self.clone.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.clone)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        (self.clone / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "initial", cwd=self.clone)
        self.checkout = tmp_path / "agent-staged"
        _run_git("worktree", "add", "-q", "-b", "feat", str(self.checkout), cwd=self.clone)
        (self.checkout / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        _run_git("add", "tracked.py", cwd=self.checkout)

    def _worktree_row(self) -> Worktree:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/1")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path=str(self.checkout),
            branch="feat",
            extra={"worktree_path": str(self.checkout)},
        )

    def _deciders(self) -> dict[str, Callable[[], bool]]:
        target = _EffectiveTarget(ref="HEAD", probe_repo=str(self.checkout), branch_to_delete="feat", label="feat")
        return {
            "cleanup.working_tree_dirt.real_uncommitted_reasons": lambda: bool(
                real_uncommitted_reasons(str(self.checkout), target)
            ),
            "cleanup.unshipped_work.probe_unshipped_work": lambda: probe_unshipped_work(self.checkout).exists,
            "cleanup.orphan_checkouts.orphan_is_dirty": lambda: orphan_is_dirty(str(self.checkout)),
            "_workspace.cleanup._worktree_clean": lambda: not _worktree_clean(str(self.checkout)),
            "models.ticket_worktree_checks.worktree_tracked_dirty_path": lambda: bool(
                worktree_tracked_dirty_path(self._worktree_row())
            ),
            "handover_orchestration._has_pending_work": lambda: _has_pending_work(self.checkout),
            "loop.worktree_gc.git_dirty": lambda: git_dirty(self.checkout),
            "cli.update._tracked_dirty_paths": lambda: bool(update_tracked_dirty_paths(self.checkout)),
            "scanners.pull_main_clone._tracked_dirty_paths": lambda: bool(pull_tracked_dirty_paths(self.checkout)),
            "scanners.self_update._tracked_dirty_paths": lambda: bool(self_update_tracked_dirty_paths(self.checkout)),
        }

    def test_fixture_is_invisible_to_a_bare_git_diff(self) -> None:
        bare = subprocess.run(
            [_GIT, "-C", str(self.checkout), "diff"], check=True, capture_output=True, text=True, env=_clean_env()
        ).stdout

        assert bare == "", "fixture invalid — the staged delta must be invisible to a bare `git diff`"

    def test_every_decider_reports_the_staged_only_checkout_as_dirty(self) -> None:
        blind = [name for name, decide in self._deciders().items() if not decide()]

        assert blind == [], "these decide from the working tree alone and miss staged work"
