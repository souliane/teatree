"""Provision a cold-review worktree at the EXACT pushed head, or hard-fail (#2132).

The cold-review bug: a reviewer told to ``git worktree add <dir> origin/<branch>``
hit a collision when that branch was already checked out in another worktree on
the same machine. The ``worktree add`` failed and the agent silently fell back to
a pre-existing checkout one commit behind the pushed head, reviewing a stale tree
and producing a spurious CHANGES_NEEDED.

:func:`add_review_worktree_at_head` is the verify-or-fail seam the review path
uses instead of a raw ``worktree add <branch>``: a detached ``FETCH_HEAD``
checkout that cannot collide with a branch worktree, plus a head-SHA assertion
that raises rather than ever returning a tree it could not prove is the reviewed
head.
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from teatree.utils.git import head_sha, worktree_remove
from teatree.utils.run import run_checked

#: The mkdtemp prefix every review checkout carries. The reaper (#4576) matches on
#: it, so creator and reaper can never disagree about which registrations are ours.
REVIEW_WORKTREE_PREFIX = "t3-review-"


class StaleReviewCheckoutError(RuntimeError):
    """The review worktree's HEAD did not match the expected PR head SHA.

    Raised by :func:`add_review_worktree_at_head` instead of silently falling
    back to a possibly-stale tree — a cold review must run against the exact
    pushed head or not at all (#2132).
    """


def add_review_worktree_at_head(
    repo: str,
    *,
    ref: str,
    expected_sha: str,
    remote: str = "origin",
    base_dir: str | None = None,
) -> str:
    """Materialise a review worktree at the EXACT pushed head, or hard-fail (#2132).

    Forecloses both halves of the stale-checkout path:

    1. It fetches ``ref`` from ``remote`` and checks it out with ``git worktree
    add --detach <dir> FETCH_HEAD`` into a guaranteed-unique temp dir under
    ``base_dir`` (default: the system temp dir). A detached ``FETCH_HEAD``
    checkout binds no branch, so it can never collide with an existing branch
    worktree — the failure mode that triggered the stale fallback disappears.
    2. After the add, it asserts ``git rev-parse HEAD`` equals ``expected_sha``
    and raises :class:`StaleReviewCheckoutError` on any divergence, removing the
    bad worktree first. It never returns a tree it could not prove is the
    reviewed head.

    Returns the absolute path of the worktree to review. An IN-PROCESS caller
    should prefer :func:`review_worktree_at_head`, which removes it unconditionally;
    an out-of-process one releases it with ``t3 review release <path>``.
    """
    run_checked(["git", "-C", repo, "fetch", remote, ref])
    worktree_dir = tempfile.mkdtemp(prefix=REVIEW_WORKTREE_PREFIX, dir=base_dir)
    run_checked(["git", "-C", repo, "worktree", "add", "--detach", worktree_dir, "FETCH_HEAD"])
    actual_sha = head_sha(worktree_dir)
    if actual_sha != expected_sha:
        worktree_remove(repo=repo, path=worktree_dir)
        message = (
            f"review checkout HEAD {actual_sha[:12]} != expected PR head {expected_sha[:12]} "
            f"(ref {ref!r}); refusing to review a divergent tree"
        )
        raise StaleReviewCheckoutError(message)
    return worktree_dir


@contextmanager
def review_worktree_at_head(
    repo: str,
    *,
    ref: str,
    expected_sha: str,
    remote: str = "origin",
    base_dir: str | None = None,
) -> Iterator[str]:
    """:func:`add_review_worktree_at_head` whose worktree is removed on every exit (#4576).

    An in-process caller cannot leak a registration through this seam: a review that
    raises, returns early, or is cancelled still deregisters. The out-of-process
    ``t3 review checkout`` cannot use it — it exits before the review begins — which
    is why the sweep in
    :mod:`teatree.core.worktree.review_worktree_reaper` is the guarantee and this is
    only the fast path.
    """
    worktree = add_review_worktree_at_head(repo, ref=ref, expected_sha=expected_sha, remote=remote, base_dir=base_dir)
    try:
        yield worktree
    finally:
        worktree_remove(repo=repo, path=worktree)
