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

import pytest
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


def _break_the_repo(clone: Path) -> None:
    """Corrupt *clone* so ``git worktree list`` really exits non-zero.

    Removing ``HEAD`` leaves ``.git`` a DIRECTORY — so the clone is still a
    candidate — while every git command against it exits 128 with "not a git
    repository". A mocked ``CommandFailedError`` cannot stand in for this: the
    defect was that no exception was ever raised on the real path.
    """
    (clone / ".git" / "HEAD").unlink()


class TestCheckoutRegistry(TestCase):
    def setUp(self) -> None:
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clone = make_git_repo(self.workspace / "org" / "repo")
        self.outside = self.workspace / "not-a-clone"
        self.outside.mkdir()
        self.enterContext(patch(f"{_REGISTRY}.Path.cwd", return_value=self.outside))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))

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

    def test_a_genuinely_broken_clone_records_a_gap(self) -> None:
        """THE fail-closed property, driven through a REAL broken repo — no mocked exception.

        ``git.run`` passes ``expected_codes=None``, so it never raises: the
        ``except CommandFailedError`` guarding this was unreachable in production
        and a failing registry returned a silently-short list with
        ``complete=True``. A short keep-set means MORE deletions, so the reaper
        failed OPEN — the exact opposite of its stated contract.
        """
        self._register(self.clone)
        _break_the_repo(self.clone)

        registry = live_checkout_paths(self.workspace)

        assert not registry.complete, "a broken clone registry must record a gap, never read as complete"
        assert any(str(self.clone) in gap for gap in registry.gaps)

    def test_control_a_healthy_clone_reads_complete(self) -> None:
        """The control proving the probe above can distinguish broken from healthy."""
        self._register(self.clone)

        registry = live_checkout_paths(self.workspace)

        assert registry.complete
        assert registry.gaps == ()

    def test_raw_worktree_paths_raises_on_a_broken_repo(self) -> None:
        """The primitive itself must raise — the gap recording downstream depends on it."""
        _break_the_repo(self.clone)

        with pytest.raises(CommandFailedError):
            raw_worktree_paths(str(self.clone))

    def test_a_checkout_whose_clone_is_undiscoverable_is_still_found(self) -> None:
        """Clone discovery is not the keep-set's foundation — the filesystem is.

        ``candidate_clones`` finds a clone only via a ``Worktree`` row or the cwd,
        so on the host it discovered 1 of the clones actually present. A checkout
        under an undiscoverable clone contributed nothing to the keep-set AND no
        gap, so its env dir looked dead. The slug is ``sha256(checkout_path)``, so
        whether that path exists on disk is answerable directly.
        """
        checkout = self._add_checkout("orphaned-clone-checkout")

        registry = live_checkout_paths(self.workspace)

        assert candidate_clones(self.workspace) == set(), "no row, not cwd — the clone is undiscoverable"
        assert str(checkout) in registry.paths
        assert registry.complete

    def test_an_unreadable_scan_root_becomes_a_gap(self) -> None:
        """A directory the scan cannot read hides an unknown number of checkouts."""
        registry = live_checkout_paths(self.workspace / "does-not-exist")

        with patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace / "absent",)):
            missing = live_checkout_paths(self.workspace)

        assert registry is not None
        assert not missing.complete
        assert any("absent" in gap for gap in missing.gaps)
