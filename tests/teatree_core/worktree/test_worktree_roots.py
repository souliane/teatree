"""The canonical worktree ROOTS the reaper and doctor scan (souliane/teatree#3583).

Real ``Worktree`` rows and real on-disk git checkouts under ``tmp_path`` — the
``probe_checkout`` probe runs the real ``git rev-parse`` and the namespace-split
classifier compares real paths against a pinned canonical root.
"""

import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.worktree_roots import (
    CheckoutState,
    canonical_worktree_root,
    probe_checkout,
    recorded_checkout_is_live,
    recorded_checkout_unreadable_reason,
    registered_worktree_roots,
    scanned_worktree_roots,
    worktrees_outside_the_canonical_root,
)
from teatree.utils import git
from tests._mount_table import missing_mount_table, patched_mount_table


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


class ProbeCheckoutTest(_RootsTestCase):
    def test_a_real_checkout_is_proven_live(self) -> None:
        checkout = self.tmp / "live"
        checkout.mkdir()
        git.run(repo=str(checkout), args=["init", "--quiet", "--initial-branch=main"])
        assert probe_checkout(checkout) is CheckoutState.CHECKOUT
        assert recorded_checkout_is_live(str(checkout), clone=None)

    def test_a_non_repo_dir_is_proven_not_a_checkout(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        assert probe_checkout(plain) is CheckoutState.NOT_A_CHECKOUT

    def test_a_dangling_gitfile_proves_nothing_and_is_never_called_dead(self) -> None:
        # git answers "not a git repository" here in the same words it uses for a
        # dir that never held one — but this dir CLAIMS to be a checkout, and the
        # admin dir it claims may exist in whatever context created it (#3912).
        unresolvable = self.tmp / "unresolvable"
        unresolvable.mkdir()
        (unresolvable / ".git").write_text("gitdir: /nonexistent/clone/.git/worktrees/gone\n", encoding="utf-8")
        assert probe_checkout(unresolvable) is CheckoutState.INCONCLUSIVE
        assert not recorded_checkout_is_live(str(unresolvable), clone=None)
        assert "execution context" in recorded_checkout_unreadable_reason(unresolvable)

    def test_an_absent_path_proves_nothing(self) -> None:
        # "cannot change to <path>" says nothing about whether a repo lives there.
        assert probe_checkout(self.tmp / "nowhere") is CheckoutState.INCONCLUSIVE

    def test_a_refusal_git_could_not_answer_is_inconclusive_not_proof(self) -> None:
        # The whole point of the third value: a dubious-ownership refusal must
        # never read as "this dir holds nothing", which is what authorises a wipe.
        refusal = mock.Mock(returncode=128, stdout="", stderr="fatal: detected dubious ownership in repository")
        with mock.patch("teatree.core.worktree.worktree_roots.run_allowed_to_fail", return_value=refusal):
            assert probe_checkout(self.tmp) is CheckoutState.INCONCLUSIVE

    def test_a_probe_that_cannot_launch_is_inconclusive(self) -> None:
        with mock.patch("teatree.core.worktree.worktree_roots.run_allowed_to_fail", side_effect=OSError("no git")):
            assert probe_checkout(self.tmp) is CheckoutState.INCONCLUSIVE


class WorktreesOutsideCanonicalRootTest(_RootsTestCase):
    def test_a_symlink_outside_the_root_pointing_inside_stays_outside_without_an_alias(self) -> None:
        inside = self.canonical / "T1" / "repo"
        inside.mkdir(parents=True)
        link = self.tmp / "external-link"
        link.symlink_to(inside, target_is_directory=True)
        row = self._register(link, branch="external-link")
        with missing_mount_table():
            assert [found.pk for found in worktrees_outside_the_canonical_root()] == [row.pk]
        with patched_mount_table(("259:2", "/ws", str(self.canonical))):
            assert [found.pk for found in worktrees_outside_the_canonical_root()] == [row.pk]

    def test_a_row_under_another_mount_spelling_of_the_canonical_root_is_inside(self) -> None:
        alias = Path("/nonexistent-t3/alias/T1/repo")
        row_alias = self._register(alias, branch="alias")
        row_elsewhere = self._register(self.tmp / "elsewhere" / "repo", branch="elsewhere")
        with patched_mount_table(
            ("259:2", "/ws", str(self.canonical)),
            ("259:2", "/ws", "/nonexistent-t3/alias"),
            ("259:2", "/other", str(self.tmp / "elsewhere")),
        ):
            assert [row.pk for row in worktrees_outside_the_canonical_root()] == [row_elsewhere.pk]
        with missing_mount_table():
            assert {row.pk for row in worktrees_outside_the_canonical_root()} == {row_alias.pk, row_elsewhere.pk}

    def test_a_symlinked_canonical_root_accepts_its_mount_alias(self) -> None:
        link = self.tmp / "canonical-link"
        link.symlink_to(self.canonical, target_is_directory=True)
        alias = Path("/nonexistent-t3/alias/T1/repo")
        self._register(alias, branch="symlink-alias")
        with (
            mock.patch("teatree.core.worktree.worktree_roots.worktree_root", return_value=link),
            patched_mount_table(("259:2", "/ws", str(self.canonical)), ("259:2", "/ws", "/nonexistent-t3/alias")),
        ):
            assert worktrees_outside_the_canonical_root() == []

    def test_a_dotdot_escape_from_the_canonical_root_is_listed(self) -> None:
        escape = self.canonical / ".." / "elsewhere" / "T1" / "repo"
        row = self._register(escape, branch="escape")
        assert [found.pk for found in worktrees_outside_the_canonical_root()] == [row.pk]

    def test_dotdot_in_the_canonical_root_is_normalized(self) -> None:
        self._register(self.canonical / "T1" / "repo", branch="normalized")
        elsewhere = self.tmp / "elsewhere" / "nested"
        elsewhere.mkdir(parents=True)
        link = self.tmp / "link"
        link.symlink_to(elsewhere, target_is_directory=True)
        with (
            mock.patch(
                "teatree.core.worktree.worktree_roots.worktree_root",
                return_value=link / ".." / "canonical",
            ),
            missing_mount_table(),
        ):
            assert worktrees_outside_the_canonical_root() == []

    def test_a_deeper_mount_on_another_device_stays_outside(self) -> None:
        row = self._register(Path("/nonexistent-t3/alias/T1/repo"), branch="deeper")
        aliased = (("259:2", "/ws", str(self.canonical)), ("259:2", "/ws", "/nonexistent-t3/alias"))
        with patched_mount_table(*aliased):
            assert worktrees_outside_the_canonical_root() == []
        with patched_mount_table(*aliased, ("259:3", "/other", "/nonexistent-t3/alias/T1")):
            assert [found.pk for found in worktrees_outside_the_canonical_root()] == [row.pk]
        with patched_mount_table(*aliased, ("259:3", "/other", str(self.canonical / "T1"))):
            assert [found.pk for found in worktrees_outside_the_canonical_root()] == [row.pk]

    def test_a_bind_of_a_subdirectory_is_inside(self) -> None:
        self._register(Path("/nonexistent-t3/branch/repo"), branch="subdir")
        with patched_mount_table(
            ("259:2", "/ws", str(self.canonical)),
            ("259:2", "/ws/T1", "/nonexistent-t3/branch"),
        ):
            assert [found.pk for found in worktrees_outside_the_canonical_root()] == []

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
