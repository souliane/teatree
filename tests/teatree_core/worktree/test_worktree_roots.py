"""The canonical worktree ROOTS the reaper and doctor scan (souliane/teatree#3583).

Real ``Worktree`` rows and real on-disk git checkouts under ``tmp_path`` — the
``resolves_as_git_checkout`` probe runs the real ``git rev-parse`` and the
namespace-split classifier compares real paths against a pinned canonical root.
"""

import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.worktree_roots import (
    canonical_worktree_root,
    registered_worktree_roots,
    resolves_as_git_checkout,
    scanned_worktree_roots,
    worktrees_outside_the_canonical_root,
)
from teatree.utils import git
from teatree.utils.run import CommandFailedError


class _RootsTestCase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.canonical = self.tmp / "canonical"
        self.canonical.mkdir()
        patch = mock.patch(
            "teatree.core.worktree.worktree_roots.worktree_root",
            return_value=self.canonical,
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _register(self, path: Path, *, branch: str) -> Worktree:
        ticket = Ticket.objects.create(issue_url=f"https://example.invalid/org/repo/issues/{branch}")
        return Worktree.objects.create(
            ticket=ticket,
            overlay="",
            repo_path="org/repo",
            branch=branch,
            extra={"worktree_path": str(path)},
        )


class ResolvesAsGitCheckoutTest(_RootsTestCase):
    def test_a_real_checkout_resolves(self) -> None:
        checkout = self.tmp / "live"
        checkout.mkdir()
        git.run(repo=str(checkout), args=["init", "--quiet", "--initial-branch=main"])
        assert resolves_as_git_checkout(checkout) is True

    def test_a_non_repo_dir_does_not_resolve(self) -> None:
        # git rev-parse raises CommandFailedError inside a plain dir — the probe
        # maps that to False rather than propagating the error.
        plain = self.tmp / "plain"
        plain.mkdir()
        assert resolves_as_git_checkout(plain) is False

    def test_an_absent_path_does_not_resolve(self) -> None:
        assert resolves_as_git_checkout(self.tmp / "nowhere") is False

    def test_a_git_probe_error_reads_as_not_a_checkout(self) -> None:
        # A raising git probe (corrupt repo, launch failure) maps to not-a-checkout
        # rather than propagating — the reaper/doctor never crash on a bad dir.
        checkout = self.tmp / "corrupt"
        checkout.mkdir()
        with mock.patch.object(git, "run", side_effect=CommandFailedError(["git", "rev-parse"], 128, "", "fatal")):
            assert resolves_as_git_checkout(checkout) is False


class WorktreesOutsideCanonicalRootTest(_RootsTestCase):
    def test_only_the_outside_worktrees_are_returned(self) -> None:
        inside = self.canonical / "1234" / "repo"
        self._register(inside, branch="inside")
        outside = self.tmp / "elsewhere" / "repo"
        row_outside = self._register(outside, branch="outside")

        result = worktrees_outside_the_canonical_root()

        assert [w.pk for w in result] == [row_outside.pk]

    def test_a_pathless_row_is_ignored(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.invalid/org/repo/issues/none")
        Worktree.objects.create(ticket=ticket, overlay="", repo_path="org/repo", branch="none", extra={})
        assert worktrees_outside_the_canonical_root() == []


class RootSetTest(_RootsTestCase):
    def test_canonical_root_is_the_configured_worktree_root(self) -> None:
        assert canonical_worktree_root() == self.canonical

    def test_registered_roots_are_the_parents_of_each_checkout(self) -> None:
        self._register(self.canonical / "1" / "repo", branch="a")
        self._register(self.tmp / "alt" / "repo", branch="b")
        roots = registered_worktree_roots()
        assert (self.canonical / "1") in roots
        assert (self.tmp / "alt") in roots

    def test_scanned_roots_lead_with_canonical_and_dedupe(self) -> None:
        self._register(self.tmp / "alt" / "repo", branch="b")
        roots = scanned_worktree_roots(self.canonical)
        # canonical first, no duplicate even though workspace == canonical here.
        assert roots[0] == self.canonical
        assert len(roots) == len(set(roots))
        assert (self.tmp / "alt") in roots
