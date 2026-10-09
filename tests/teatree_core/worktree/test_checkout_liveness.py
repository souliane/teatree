"""A checkout whose gitdir pointer resolves only in its creating context is LIVE (#3912, #3853).

Functional throughout: a real clone, a real ``git worktree add``, and the real
``git rev-parse`` probe. The clone is then MOVED, which is all it takes to
reproduce the defect — the checkout's ``.git`` file still records the absolute
admin dir its creator wrote, that path no longer exists, and ``git`` answers
``fatal: not a git repository`` in the same words it uses for a directory that
never held a repository at all.

The bar these tests hold: a probe that cannot answer reports UNKNOWN, and only a
directory positively proven never to have been a checkout may authorise deletion.
"""

import shutil
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.worktree.checkout_liveness import (
    admin_entry_for,
    claims_to_be_a_checkout,
    context_scoped_pointer,
    read_gitdir_pointer,
)
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout
from teatree.utils import git


class _RelocatedCloneTestCase(TestCase):
    """A checkout created against a clone that has since moved out from under it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.creating_context = self.tmp / "creating-context" / "repo"
        self.creating_context.mkdir(parents=True)
        git.run(repo=str(self.creating_context), args=["init", "--quiet", "--initial-branch=main"])
        git.run(repo=str(self.creating_context), args=["config", "user.email", "test@example.com"])
        git.run(repo=str(self.creating_context), args=["config", "user.name", "Test"])
        git.run(repo=str(self.creating_context), args=["commit", "--quiet", "--allow-empty", "-m", "base"])

        self.checkout = self.tmp / "checkouts" / "feature-wt"
        git.run(
            repo=str(self.creating_context),
            args=["worktree", "add", "--quiet", str(self.checkout), "-b", "feature"],
        )
        assert probe_checkout(self.checkout) is CheckoutState.CHECKOUT, "fixture must start live"

        # The one move that reproduces it: the clone is reachable HERE at a
        # different absolute path than the one the checkout recorded.
        self.clone = self.tmp / "this-context" / "repo"
        self.clone.parent.mkdir(parents=True)
        shutil.move(str(self.creating_context), str(self.clone))


class ContextScopedPointerTest(_RelocatedCloneTestCase):
    def test_the_recorded_admin_dir_is_absent_from_this_context(self) -> None:
        pointer = read_gitdir_pointer(self.checkout)

        assert pointer is not None
        assert not pointer.resolves_here
        assert pointer.entry_name == "feature-wt"

    def test_a_checkout_that_carries_a_git_entry_claims_to_be_one(self) -> None:
        assert claims_to_be_a_checkout(self.checkout)

    def test_a_plain_directory_claims_nothing(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()

        assert not claims_to_be_a_checkout(plain)
        assert read_gitdir_pointer(plain) is None
        assert context_scoped_pointer(plain) is None

    def test_a_live_checkout_has_no_context_scoped_pointer(self) -> None:
        live = self.tmp / "live"
        live.mkdir()
        git.run(repo=str(live), args=["init", "--quiet", "--initial-branch=main"])

        assert context_scoped_pointer(live) is None

    def test_a_git_file_that_is_not_a_gitdir_pointer_reads_as_no_pointer(self) -> None:
        junk = self.tmp / "junk"
        junk.mkdir()
        (junk / ".git").write_text("this is not a gitfile\n", encoding="utf-8")

        assert read_gitdir_pointer(junk) is None


class AdminEntryResolutionTest(_RelocatedCloneTestCase):
    """Proof of LIFE comes from the clone's entry NAME, never the recorded path."""

    def test_the_visible_clone_still_holds_the_checkout_entry(self) -> None:
        entry = admin_entry_for(self.checkout, self.clone)

        assert entry is not None
        assert entry == self.clone / ".git" / "worktrees" / "feature-wt"

    def test_a_clone_with_no_such_entry_vouches_for_nothing(self) -> None:
        unrelated = self.tmp / "unrelated"
        unrelated.mkdir()
        git.run(repo=str(unrelated), args=["init", "--quiet", "--initial-branch=main"])

        assert admin_entry_for(self.checkout, unrelated) is None

    def test_a_same_named_entry_for_a_different_checkout_does_not_vouch(self) -> None:
        # The entry name alone is not enough: it must point back at a checkout of
        # THIS checkout's name, or an unrelated clone's coincidence would read as
        # proof of life for a directory it has never heard of.
        entry = self.clone / ".git" / "worktrees" / "feature-wt" / "gitdir"
        entry.write_text("/somewhere/else/other-wt/.git\n", encoding="utf-8")

        assert admin_entry_for(self.checkout, self.clone) is None

    def test_a_directory_naming_no_admin_dir_cannot_be_vouched_for(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()

        assert admin_entry_for(plain, self.clone) is None


class ProbeRefusesToCallItDeadTest(_RelocatedCloneTestCase):
    def test_a_context_scoped_checkout_is_never_reported_dead(self) -> None:
        # The regression. Judged with no clone to consult, the honest answer is
        # UNKNOWN — never NOT_A_CHECKOUT, which is the only state that authorises
        # a release or a directory wipe.
        assert probe_checkout(self.checkout) is not CheckoutState.NOT_A_CHECKOUT
        assert probe_checkout(self.checkout) is CheckoutState.INCONCLUSIVE

    def test_the_clone_this_context_can_see_proves_the_checkout_live(self) -> None:
        assert probe_checkout(self.checkout, clone=self.clone) is CheckoutState.CHECKOUT

    def test_a_clone_holding_no_such_admin_entry_does_not_upgrade_the_verdict(self) -> None:
        unrelated = self.tmp / "unrelated"
        unrelated.mkdir()
        git.run(repo=str(unrelated), args=["init", "--quiet", "--initial-branch=main"])

        assert probe_checkout(self.checkout, clone=unrelated) is CheckoutState.INCONCLUSIVE

    def test_a_directory_that_never_held_a_checkout_stays_provably_dead(self) -> None:
        # The fail-open discipline must not cost the probe its one positive proof:
        # a dir carrying no `.git` at all never claimed to be a checkout.
        plain = self.tmp / "never-a-checkout"
        plain.mkdir()

        assert probe_checkout(plain, clone=self.clone) is CheckoutState.NOT_A_CHECKOUT
