"""#3977: has the branch's work already reached the base, and what would its PR do to it?

Every other layer in :mod:`teatree.core.worktree.branch_classification` compares
PATHS — the subject prefilter, ``git cherry``'s patch-id (which hashes the
``diff --git a/… b/…`` header), the squash-tree probe. All three go blind the
moment the base takes the same change under a different path: a module split
lands the fix and its byte-identical tests elsewhere, and the branch reads as
still owing a pull request on every tick, forever.

Git is content-addressed, so the question has an exact answer the path
comparisons cannot give: is every BLOB the branch introduces already in the base
tree, at any path at all? That is :func:`branch_content_landed_on_base`, and it
is what discharges an owed :class:`~teatree.core.models.pending_pull_request.PendingPullRequest`
instead of renewing it.

:func:`assess_revert_risk` answers the second half. A branch far enough behind a
base refactor produces a would-be PR whose diff against the current base is
dominated by deletions of work the branch never touched — the shape that needs a
rebase decision from a person, never a bare "open a PR" instruction.

Both probes fail CLOSED: any git error reports "still owes" / "no risk measured",
because a false discharge destroys the only record of unshipped work (the same
data-loss doctrine the worktree reapers hold).
"""

from dataclasses import dataclass

from teatree.utils import git
from teatree.utils.run import CommandFailedError

#: Net lines of base content a would-be PR must remove before the remedy stops
#: urging "open a PR" and asks for a rebase decision instead. Sized like
#: :data:`~teatree.core.models.pending_pull_request.MAX_DRAIN_ATTEMPTS`: low enough that a branch
#: sitting behind a real refactor trips it, high enough that ordinary drift on an
#: active default branch does not.
REVERT_RISK_NET_REMOVED_LINES = 200


@dataclass(frozen=True, slots=True)
class RevertRisk:
    """What a pull request opened from this branch would do to the CURRENT base.

    ``measured`` separates a genuinely risk-free branch from one whose repo could
    not be read — only a real measurement may ever set :attr:`at_risk`.
    """

    files_changed: int = 0
    added: int = 0
    removed: int = 0
    measured: bool = False

    @property
    def net_removed(self) -> int:
        return self.removed - self.added

    @property
    def at_risk(self) -> bool:
        return self.measured and self.net_removed >= REVERT_RISK_NET_REMOVED_LINES


def _tree_entries(repo: str, ref: str) -> dict[str, str]:
    """Map each path in ``ref``'s tree to its object id, raising on an unreadable ref.

    ``-z`` is what makes the parse exact: without it git quotes and escapes any
    path holding a space or a non-ASCII byte, so those entries would silently
    land under a mangled key and read as absent.
    """
    raw = git.run_strict(repo=repo, args=["ls-tree", "-r", "-z", ref])
    entries: dict[str, str] = {}
    for record in raw.split("\0"):
        meta, tab, path = record.partition("\t")
        if not tab:
            continue
        entries[path] = meta.split()[-1]
    return entries


def branch_content_landed_on_base(repo: str, branch: str, target: str) -> bool:
    """Whether every change ``branch`` carries is already on ``target``, at ANY path.

    Two halves, both judged against the branch's merge-base so only the branch's
    own contribution is weighed:

    - what it INTRODUCES is matched by blob id alone, so a fix the base took
        under a different path (the refactor case) counts as landed;
    - what it REMOVES is matched by ``(path, blob)``, because a removal has only
        landed when that exact content is no longer at that exact path. Blob-only
        matching would read a branch whose entire work is a deletion as landed —
        it introduces nothing — and discharge an obligation that is still real.

    A branch with NO tree delta at all is never landed. "Introduced nothing
    anywhere" is not evidence the work reached the base; it is evidence there was
    no content to reach it, and such a branch's commits are still the only record
    of themselves — the orphan guard tracks them.

    Fails CLOSED on any git error: an unreadable ref never discharges.
    """
    try:
        merge_base = git.run_strict(repo=repo, args=["merge-base", target, branch])
        base = _tree_entries(repo, target)
        tip = _tree_entries(repo, branch)
        forked_from = _tree_entries(repo, merge_base)
    except CommandFailedError:
        return False

    introduced = {blob for path, blob in tip.items() if forked_from.get(path) != blob}
    withdrawn = {(path, blob) for path, blob in forked_from.items() if tip.get(path) != blob}
    if not introduced and not withdrawn:
        return False
    if not introduced.issubset(set(base.values())):
        return False
    return all(base.get(path) != blob for path, blob in withdrawn)


def assess_revert_risk(repo: str, branch: str, target: str) -> RevertRisk:
    """Measure the two-dot ``target..branch`` diff — what a PR from ``branch`` would remove.

    Two-dot, not three-dot: the question is what the branch's tree lacks relative
    to the base as it stands NOW, which is exactly the base work a stale branch
    would take back out. A binary row counts as a file, not as lines — git
    reports its counts as ``-``.
    """
    try:
        raw = git.run_strict(repo=repo, args=["diff", "--numstat", target, branch])
    except CommandFailedError:
        return RevertRisk()
    files = added = removed = 0
    for line in raw.splitlines():
        added_field, _, rest = line.partition("\t")
        removed_field, _, _path = rest.partition("\t")
        files += 1
        added += int(added_field) if added_field.isdigit() else 0
        removed += int(removed_field) if removed_field.isdigit() else 0
    return RevertRisk(files_changed=files, added=added, removed=removed, measured=True)


__all__ = ["REVERT_RISK_NET_REMOVED_LINES", "RevertRisk", "assess_revert_risk", "branch_content_landed_on_base"]
