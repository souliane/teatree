"""May a registered worktree whose checkout is DEAD be released? (souliane/teatree#3583 follow-up).

The row reaper's ordinary safety step, :func:`worktree_done.analyze_worktree_changes`,
runs its working-tree dirt probe INSIDE the worktree dir. When that dir no longer
resolves as a git repo the probe can never answer, so it fails closed forever and
the row is kept for a salvage that can never run — while the broken-DIR reaper
skips the same dir because a row still tracks it. Two passes, each deferring to
the other, and `t3 doctor` prescribing the one command that could not act.

This module is the missing owner. For a provably-dead checkout the question moves
off the unreadable dir and onto the BRANCH in the source clone, which is where any
recoverable git work actually lives: the checkout's admin entry is gone, but
``refs/heads/<branch>`` and its reflog are not. Release requires positive proof on
both halves — the dir is provably not a repo (:data:`CheckoutState.NOT_A_CHECKOUT`,
never a probe that merely errored) AND the branch carries nothing that exists on no
remote. Anything short of that keeps the row and names why.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.core.cleanup.cleanup import _resolve_worktree_path
from teatree.core.models import Worktree
from teatree.core.worktree.branch_classification import content_equivalence_blockers, effective_default_target
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout
from teatree.utils import git
from teatree.utils.run import CommandFailedError

_PREVIEW_LIMIT = 3


class BrokenCheckout(StrEnum):
    """The disposition of one registered worktree's on-disk checkout."""

    LIVE_CHECKOUT = "live-checkout"
    UNVERIFIABLE = "unverifiable"
    HOLDS_WORK = "holds-work"
    RELEASABLE = "releasable"


@dataclass(frozen=True, slots=True)
class BrokenCheckoutVerdict:
    """A :class:`BrokenCheckout` state plus the phrase the reaper reports for it."""

    state: BrokenCheckout
    reason: str = ""


def classify_broken_checkout(worktree: Worktree, *, workspace: Path) -> BrokenCheckoutVerdict:
    """Decide whether *worktree*'s row may be released because its checkout is dead.

    ``LIVE_CHECKOUT`` means this pass has no business here — the dir is a working
    checkout, or it is absent entirely (an ordinary reaped worktree the done pass
    owns). The other three states all describe a dir that EXISTS but is not a repo.
    """
    wt_path = Path(_resolve_worktree_path(workspace, worktree))
    if not wt_path.is_dir():
        return BrokenCheckoutVerdict(BrokenCheckout.LIVE_CHECKOUT)
    probe = probe_checkout(wt_path)
    if probe is CheckoutState.CHECKOUT:
        return BrokenCheckoutVerdict(BrokenCheckout.LIVE_CHECKOUT)
    if probe is CheckoutState.INCONCLUSIVE:
        return BrokenCheckoutVerdict(
            BrokenCheckout.UNVERIFIABLE,
            f"git could not say whether {wt_path} is a checkout — keeping until it can",
        )
    return _branch_verdict(worktree, workspace=workspace, wt_path=wt_path)


def _branch_verdict(worktree: Worktree, *, workspace: Path, wt_path: Path) -> BrokenCheckoutVerdict:
    """Judge the dead checkout by its branch in the source clone.

    The checkout is proven dead at this point, so the only work that can still be
    recovered is what the clone holds. A clone that cannot be resolved leaves that
    unanswerable, which is a KEEP — the same fail-closed posture the #706 guard
    takes on an inconclusive probe.
    """
    repo_main = resolve_clone_path(workspace, worktree)
    if repo_main is None or not repo_main.is_dir():
        return BrokenCheckoutVerdict(
            BrokenCheckout.UNVERIFIABLE,
            f"the source clone for {wt_path} is unresolvable, so the branch's push state is unknown — keeping",
        )
    branch = worktree.branch
    if not branch or not _branch_ref_exists(repo_main, branch):
        return BrokenCheckoutVerdict(BrokenCheckout.RELEASABLE, _releasable_reason(wt_path, "its branch ref is gone"))
    blockers = _unrecoverable_work(repo_main, branch)
    if blockers:
        return BrokenCheckoutVerdict(
            BrokenCheckout.HOLDS_WORK,
            f"the checkout at {wt_path} is dead but '{branch}' still holds {blockers} — "
            "push it or `t3 <overlay> workspace salvage`, do not release the row",
        )
    return BrokenCheckoutVerdict(
        BrokenCheckout.RELEASABLE, _releasable_reason(wt_path, f"'{branch}' holds nothing that is on no remote")
    )


def _releasable_reason(wt_path: Path, branch_clause: str) -> str:
    return f"the checkout at {wt_path} is provably not a git repo and {branch_clause}"


def _branch_ref_exists(repo_main: Path, branch: str) -> bool:
    return git.check(repo=str(repo_main), args=["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])


def _unrecoverable_work(repo_main: Path, branch: str) -> str:
    """Describe *branch*'s work that would be lost by a release; ``""`` when nothing would.

    The #706 standard, not the stricter ``origin/main`` hygiene one: commits that
    exist on some remote survive the branch's deletion, so they never block. What
    blocks is a commit on NO remote whose CONTENT is also not upstream — and, fail
    closed, a probe that could not decide.
    """
    try:
        unpushed = git.commits_absent_from_all_remotes(str(repo_main), branch)
    except CommandFailedError as exc:
        return f"a push state git could not read ({exc})"
    if not unpushed:
        return ""
    if not content_equivalence_blockers(str(repo_main), branch, effective_default_target(str(repo_main))):
        return ""
    preview = ", ".join(unpushed[:_PREVIEW_LIMIT]) + (", …" if len(unpushed) > _PREVIEW_LIMIT else "")
    return f"{len(unpushed)} commit(s) on NO remote, content not upstream: {preview}"


__all__ = ["BrokenCheckout", "BrokenCheckoutVerdict", "classify_broken_checkout"]
