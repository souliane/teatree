"""Classifying an on-disk checkout without asking ``git``.

Every ``git`` query about a linked worktree goes through its ``.git`` pointer, so a
checkout whose gitdir is absent in THIS venue — a host ``git worktree add`` tree
bind-mounted into the container, whose pointer names a host-only clone path — makes
every query fail identically to "this is not a checkout at all". The resolver could
not tell those apart and reported the generic refusal for both; this leaf is the
pure-filesystem classification that separates them.
"""

from pathlib import Path

import pytest

from teatree.core.worktree.checkout_gitdir import (
    CheckoutKind,
    checkout_of_admin_entry,
    classify_checkout,
    gitdir_pointer,
)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone whose linked-worktree administrative dir exists."""
    gitdir = tmp_path / "clone" / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    return tmp_path / "clone"


def _linked(checkout: Path, gitdir: Path | str) -> Path:
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return checkout


class TestGitdirPointer:
    def test_reads_the_absolute_pointer(self, tmp_path: Path) -> None:
        checkout = _linked(tmp_path / "wt", "/clone/.git/worktrees/feature")
        assert gitdir_pointer(checkout) == Path("/clone/.git/worktrees/feature")

    def test_resolves_a_relative_pointer_against_the_checkout(self, tmp_path: Path) -> None:
        checkout = _linked(tmp_path / "wt", "../clone/.git/worktrees/feature")
        assert gitdir_pointer(checkout) == (tmp_path / "clone" / ".git" / "worktrees" / "feature").resolve()

    def test_none_for_a_clone(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "clone"
        (clone_dir / ".git").mkdir(parents=True)
        assert gitdir_pointer(clone_dir) is None

    def test_none_for_a_plain_directory(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert gitdir_pointer(plain) is None


class TestCheckoutOfAdminEntry:
    @pytest.mark.parametrize(
        "recorded",
        ["{root}/wt/.git", "../../../../wt/.git"],
        ids=["absolute", "relative-to-the-entry"],
    )
    def test_names_the_checkout_however_git_recorded_it(self, clone: Path, recorded: str) -> None:
        entry = clone / ".git" / "worktrees" / "feature"
        (entry / "gitdir").write_text(recorded.format(root=clone.parent) + "\n", encoding="utf-8")

        assert checkout_of_admin_entry(entry) == clone.parent / "wt"

    @pytest.mark.parametrize("content", [None, ""], ids=["missing", "empty"])
    def test_none_when_the_entry_names_nothing(self, clone: Path, content: str | None) -> None:
        entry = clone / ".git" / "worktrees" / "feature"
        if content is not None:
            (entry / "gitdir").write_text(content, encoding="utf-8")

        assert checkout_of_admin_entry(entry) is None


class TestClassifyCheckout:
    def test_plain_directory_is_not_a_checkout(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert classify_checkout(plain) is CheckoutKind.NOT_A_CHECKOUT

    def test_missing_directory_is_not_a_checkout(self, tmp_path: Path) -> None:
        assert classify_checkout(tmp_path / "gone") is CheckoutKind.NOT_A_CHECKOUT

    def test_main_clone_is_its_own_kind(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "clone"
        (clone_dir / ".git").mkdir(parents=True)
        assert classify_checkout(clone_dir) is CheckoutKind.MAIN_CLONE

    def test_linked_worktree_with_a_reachable_gitdir_is_adoptable(self, tmp_path: Path, clone: Path) -> None:
        checkout = _linked(tmp_path / "wt", clone / ".git" / "worktrees" / "feature")
        assert classify_checkout(checkout) is CheckoutKind.LINKED_WORKTREE

    def test_linked_worktree_whose_gitdir_is_absent_here_is_its_own_kind(self, tmp_path: Path) -> None:
        # The 2026-09-21 amendment: the pointer names a host path the container
        # never mounts, so every git query fails while `.git` itself looks fine.
        checkout = _linked(tmp_path / "wt", "/elsewhere/backend/.git/worktrees/wt")
        assert classify_checkout(checkout) is CheckoutKind.UNREACHABLE_GITDIR
