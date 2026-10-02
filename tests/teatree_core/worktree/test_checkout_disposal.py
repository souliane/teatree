"""Disposal needs positive proof — an absence this context cannot account for is not proof (#3967).

Functional throughout: a real clone, a real ``git worktree add``, and the real
gitdir pointers those write. The defect being pinned is that a checkout created in
another context carries an absolute admin-dir pointer this context cannot follow, so
it is missing from the acting clone's registration survey and reads exactly like a
partial directory a died-mid-checkout attempt left behind.

The bar: only two things authorise removal — a directory that never claimed to be a
checkout, and one the acting clone itself vouches for — and neither survives a live
writer standing in the directory.
"""

import shutil
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.models import Session, Ticket, Worktree
from teatree.core.worktree.checkout_disposal import disposal_refusal
from teatree.utils import git


class _CheckoutFixture(TestCase):
    """A clone acting for this context, plus a second clone standing in for another."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.clone = self._clone("acting")
        self.foreign_clone = self._clone("another-context")

    def _clone(self, name: str) -> Path:
        clone = self.tmp / name / "repo"
        clone.mkdir(parents=True)
        git.run(repo=str(clone), args=["init", "--quiet", "--initial-branch=main"])
        git.run(repo=str(clone), args=["config", "user.email", "test@example.com"])
        git.run(repo=str(clone), args=["config", "user.name", "Test"])
        git.run(repo=str(clone), args=["commit", "--quiet", "--allow-empty", "-m", "base"])
        return clone

    def _checkout(self, clone: Path, name: str, branch: str) -> Path:
        checkout = self.tmp / "checkouts" / name
        git.run(repo=str(clone), args=["worktree", "add", "--quiet", str(checkout), "-b", branch])
        return checkout


class TestDisposalNeedsPositiveProof(_CheckoutFixture):
    def test_a_directory_that_never_claimed_to_be_a_checkout_is_disposable(self) -> None:
        partial = self.tmp / "checkouts" / "died-mid-add"
        partial.mkdir(parents=True)
        (partial / "half-written.txt").write_text("x\n", encoding="utf-8")

        assert disposal_refusal(partial, clone=self.clone) == ""

    def test_a_path_that_is_not_there_is_disposable(self) -> None:
        assert disposal_refusal(self.tmp / "nothing-here", clone=self.clone) == ""

    def test_a_checkout_the_acting_clone_vouches_for_is_disposable(self) -> None:
        # The ordinary leftover the reconcile step exists to reap: our own clone
        # holds its admin entry, so removing it is a decision this context owns.
        checkout = self._checkout(self.clone, "ours", "feature")

        assert disposal_refusal(checkout, clone=self.clone) == ""

    def test_a_checkout_another_clone_holds_is_refused(self) -> None:
        checkout = self._checkout(self.foreign_clone, "theirs", "feature")

        refusal = disposal_refusal(checkout, clone=self.clone)

        assert str(self.clone) in refusal
        assert "does not vouch" in refusal

    def test_a_gitdir_naming_a_root_absent_here_reports_the_view_mismatch(self) -> None:
        # The reported incident: the pointer is sound where it was written and
        # unreachable here, which git reports in the same words it uses for a
        # directory that never held a repository.
        checkout = self._checkout(self.foreign_clone, "elsewhere", "feature")
        shutil.rmtree(self.foreign_clone)

        refusal = disposal_refusal(checkout, clone=self.clone)

        assert "VIEW MISMATCH" in refusal
        assert str(self.foreign_clone) in refusal, "the refusal must name the root to act from"


class TestDisposalRefusesALiveWriter(_CheckoutFixture):
    """Occupancy is independent of every git question — and of the acting clone."""

    def _held_by_a_busy_ticket(self, path: Path) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3952", repos=["repo"])
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="repo", branch="feature", extra={"worktree_path": str(path)}
        )
        Session.objects.create(ticket=ticket, overlay="test")
        return ticket

    def test_a_checkout_our_own_clone_vouches_for_is_still_refused_while_held(self) -> None:
        checkout = self._checkout(self.clone, "ours", "feature")
        occupant = self._held_by_a_busy_ticket(checkout)

        refusal = disposal_refusal(checkout, clone=self.clone)

        assert str(occupant.pk) in refusal or occupant.ticket_number in refusal
        assert "live writer" in refusal

    def test_the_requesting_ticket_never_blocks_its_own_reprovision(self) -> None:
        # The ticket being provisioned is busy BY CONSTRUCTION — it is the reason
        # provisioning is running — so its own liveness must not wedge it forever.
        checkout = self._checkout(self.clone, "ours", "feature")
        occupant = self._held_by_a_busy_ticket(checkout)

        assert disposal_refusal(checkout, clone=self.clone, requesting_ticket_id=occupant.pk) == ""

    def test_an_idle_tickets_row_does_not_block_disposal(self) -> None:
        checkout = self._checkout(self.clone, "ours", "feature")
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1", repos=["repo"])
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="repo", branch="feature", extra={"worktree_path": str(checkout)}
        )

        assert disposal_refusal(checkout, clone=self.clone) == ""
