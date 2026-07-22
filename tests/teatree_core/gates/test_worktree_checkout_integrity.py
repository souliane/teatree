"""``find_broken_registered_checkouts`` — registered rows whose dir is a broken checkout.

A ``Worktree`` row's ``extra['worktree_path']`` should point at a live git
checkout. A dir that EXISTS but fails ``git rev-parse`` is a broken checkout the
reconciler's missing-dir finding never fires on (the dir is present), so nothing
surfaces it. This gate names each one for the ``t3 doctor`` FAIL (#3583).
"""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.gates.worktree_checkout_integrity import find_broken_registered_checkouts
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git


class TestFindBrokenRegisteredCheckouts(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _register(self, checkout: Path | None, *, branch: str = "b") -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url=f"https://example.com/i/{branch}")
        extra = {"worktree_path": str(checkout)} if checkout is not None else {}
        return Worktree.objects.create(ticket=ticket, overlay="test", repo_path="org/repo", branch=branch, extra=extra)

    def test_broken_registered_checkout_is_reported(self) -> None:
        broken = self.root / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /gone/.git/worktrees/x\n", encoding="utf-8")
        wt = self._register(broken, branch="broken")

        findings = find_broken_registered_checkouts()

        assert [f.worktree_pk for f in findings] == [wt.pk]
        assert findings[0].path == broken

    def test_healthy_registered_checkout_is_not_reported(self) -> None:
        healthy = make_git_repo(self.root / "healthy")
        self._register(healthy, branch="healthy")

        assert find_broken_registered_checkouts() == []

    def test_healthy_linked_worktree_is_not_reported(self) -> None:
        clone = make_git_repo(self.root / "clone")
        wt_dir = self.root / "linked"
        run_git(clone, "worktree", "add", str(wt_dir))
        self._register(wt_dir, branch="linked")

        assert find_broken_registered_checkouts() == []

    def test_missing_dir_is_not_reported(self) -> None:
        self._register(self.root / "never-created", branch="gone")

        assert find_broken_registered_checkouts() == []

    def test_pathless_row_is_skipped(self) -> None:
        self._register(None, branch="pathless")

        assert find_broken_registered_checkouts() == []
