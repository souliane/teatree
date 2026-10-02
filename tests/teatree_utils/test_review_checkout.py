"""Review-checkout helper lands on the EXACT pushed head, never a stale tree (#2132).

The cold-review bug: a reviewer was told ``git worktree add <dir> origin/<branch>``;
the branch was already checked out in another worktree, so ``worktree add`` failed
and the agent silently fell back to a pre-existing (stale, one commit behind)
checkout — producing a spurious CHANGES_NEEDED.

:func:`add_review_worktree_at_head` forecloses both halves:

1. it fetches the ref and adds a ``--detach FETCH_HEAD`` worktree in a
guaranteed-unique temp dir, so it can never collide with a branch worktree;
2. it asserts the materialised HEAD equals the expected PR head SHA and
HARD-FAILS (raises :class:`StaleReviewCheckoutError`) on divergence — it never
falls back to a stale tree.
"""

from pathlib import Path

import pytest

from teatree.utils.git import head_sha
from teatree.utils.review_checkout import StaleReviewCheckoutError, add_review_worktree_at_head
from tests._git_repo import make_git_repo, run_git


@pytest.fixture
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare ``origin`` with a ``feature`` branch, plus a clone that tracks it."""
    origin = make_git_repo(tmp_path / "origin.git", bare=True)

    seed = make_git_repo(tmp_path / "seed")
    (seed / "README.md").write_text("base\n")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-q", "-m", "base")
    run_git(seed, "remote", "add", "origin", str(origin))
    run_git(seed, "push", "-q", "origin", "main")
    run_git(seed, "checkout", "-q", "-b", "feature")
    (seed / "README.md").write_text("first head\n")
    run_git(seed, "commit", "-q", "-am", "feature first head")
    run_git(seed, "push", "-q", "origin", "feature")

    clone = make_git_repo(tmp_path / "clone", initial_commit=False)
    run_git(clone, "remote", "add", "origin", str(origin))
    run_git(clone, "fetch", "-q", "origin")
    return origin, clone


def _push_new_head(seed_repo: Path) -> str:
    """Advance ``feature`` by one commit and push; return the new head SHA."""
    (seed_repo / "README.md").write_text("real pushed head\n")
    run_git(seed_repo, "commit", "-q", "-am", "feature real head")
    run_git(seed_repo, "push", "-q", "origin", "feature")
    return run_git(seed_repo, "rev-parse", "HEAD")


class TestAddReviewWorktreeAtHead:
    def test_lands_on_exact_head_when_branch_already_checked_out_elsewhere(
        self, origin_and_clone: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """The collision case: ``feature`` is checked out in another worktree.

        A plain ``git worktree add <dir> feature`` would fail (branch already
        checked out) and tempt a stale fallback. ``--detach FETCH_HEAD`` must
        land on the exact pushed head regardless.
        """
        _origin, clone = origin_and_clone
        seed = tmp_path / "seed"

        # Another worktree already has ``feature`` checked out — the collision.
        colliding = tmp_path / "colliding-wt"
        run_git(clone, "worktree", "add", str(colliding), "-b", "feature", "origin/feature")

        pushed_head = _push_new_head(seed)

        review_root = tmp_path / "review-roots"
        review_root.mkdir()
        wt = add_review_worktree_at_head(str(clone), ref="feature", expected_sha=pushed_head, base_dir=str(review_root))

        assert head_sha(wt) == pushed_head
        assert (Path(wt) / "README.md").read_text() == "real pushed head\n"

    def test_hard_fails_on_divergence_never_returns_stale_tree(
        self, origin_and_clone: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A mismatch between the materialised HEAD and the expected SHA raises."""
        _origin, clone = origin_and_clone
        seed = tmp_path / "seed"
        pushed_head = _push_new_head(seed)

        wrong_expected = "0" * 40
        review_root = tmp_path / "review-roots"
        review_root.mkdir()

        with pytest.raises(StaleReviewCheckoutError) as exc:
            add_review_worktree_at_head(
                str(clone), ref="feature", expected_sha=wrong_expected, base_dir=str(review_root)
            )
        assert pushed_head[:12] in str(exc.value)
        assert wrong_expected[:12] in str(exc.value)

    def test_unique_dir_per_call_no_collision_across_invocations(
        self, origin_and_clone: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Two calls for the same ref produce two distinct worktree dirs."""
        _origin, clone = origin_and_clone
        seed = tmp_path / "seed"
        pushed_head = _push_new_head(seed)

        review_root = tmp_path / "review-roots"
        review_root.mkdir()
        first = add_review_worktree_at_head(
            str(clone), ref="feature", expected_sha=pushed_head, base_dir=str(review_root)
        )
        second = add_review_worktree_at_head(
            str(clone), ref="feature", expected_sha=pushed_head, base_dir=str(review_root)
        )
        assert first != second
        assert head_sha(first) == pushed_head
        assert head_sha(second) == pushed_head
