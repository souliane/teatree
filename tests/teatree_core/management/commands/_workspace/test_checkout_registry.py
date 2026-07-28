"""The shared "which checkouts are live?" seam both clean-all reapers ask (#3852).

Real ``git worktree`` under ``tmp_path``. The contract is not just the set of
paths but the ``gaps`` channel beside it: a clone whose registry could not be read
leaves an unknown number of live checkouts unaccounted for, and a caller with a
destructive disposition must be able to tell that apart from "there are none".
Returning a silently-short set is what made the env-dir reaper unsafe.

``Path.cwd`` is pinned throughout: :func:`candidate_clones` adds the working
directory when it is itself a main clone, which would otherwise make every
assertion depend on where the runner happens to live.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.management.commands._workspace.checkout_registry import (
    candidate_clones,
    live_checkout_paths,
    raw_worktree_paths,
)
from teatree.core.models import Ticket, Worktree
from teatree.utils.run import CommandFailedError
from tests._git_repo import make_git_repo, run_git

_REGISTRY = "teatree.core.management.commands._workspace.checkout_registry"


class TestCheckoutRegistry(TestCase):
    def setUp(self) -> None:
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clone = make_git_repo(self.workspace / "org" / "repo")
        self.outside = self.workspace / "not-a-clone"
        self.outside.mkdir()
        self.enterContext(patch(f"{_REGISTRY}.Path.cwd", return_value=self.outside))

    def _register(self, clone: Path) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url=f"https://example.com/issues/{clone.name}")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="registered",
            extra={"worktree_path": str(clone), "clone_path": str(clone)},
        )

    def _add_checkout(self, branch: str) -> Path:
        checkout = self.workspace / branch
        run_git(self.clone, "worktree", "add", "-q", "-b", branch, str(checkout))
        return checkout

    def test_a_rows_clone_path_is_a_candidate_clone(self) -> None:
        self._register(self.clone)

        assert candidate_clones(self.workspace) == {str(self.clone.resolve())}

    def test_the_cwd_is_a_candidate_only_when_it_is_itself_a_main_clone(self) -> None:
        """A linked worktree carries a ``.git`` FILE, so it is never mistaken for a clone."""
        assert candidate_clones(self.workspace) == set()

        with patch(f"{_REGISTRY}.Path.cwd", return_value=self.clone):
            assert candidate_clones(self.workspace) == {str(self.clone.resolve())}

    def test_linked_worktrees_are_reported_and_the_main_checkout_is_not(self) -> None:
        checkout = self._add_checkout("feat-a")

        worktrees = raw_worktree_paths(str(self.clone))

        assert worktrees == {str(checkout): "feat-a"}

    def test_live_paths_span_the_clone_and_its_linked_worktrees(self) -> None:
        self._register(self.clone)
        checkout = self._add_checkout("feat-b")

        registry = live_checkout_paths(self.workspace)

        assert registry.complete
        assert registry.gaps == ()
        assert {str(self.clone.resolve()), str(checkout)} <= registry.paths

    def test_a_removed_checkout_drops_out_of_the_live_set(self) -> None:
        """Anti-vacuous control: the set tracks git, rather than only ever growing."""
        self._register(self.clone)
        checkout = self._add_checkout("feat-gone")
        run_git(self.clone, "worktree", "remove", "--force", str(checkout))

        assert str(checkout) not in live_checkout_paths(self.workspace).paths

    def test_an_unreadable_clone_registry_becomes_a_gap_not_a_short_set(self) -> None:
        """The whole point of ``gaps``: silence here is what authorised a wrong deletion."""
        self._register(self.clone)
        failure = CommandFailedError(["git", "worktree", "list"], 128, "", "fatal: bad object")

        with patch(f"{_REGISTRY}.raw_worktree_paths", side_effect=failure):
            registry = live_checkout_paths(self.workspace)

        assert not registry.complete
        assert registry.paths == frozenset()
        assert any("could not list worktrees" in gap and str(self.clone) in gap for gap in registry.gaps)
