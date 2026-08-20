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
instead of renewing it. Landing is judged by NEW occurrences, not raw presence —
a blob the base already held before the branch even existed (some old, unrelated
file) never counts, no matter how many fresh copies of it a change adds; only a
blob the base GAINED since the fork proves the base actually absorbed this
branch's contribution.

:func:`assess_revert_risk` answers the second half: would a REAL merge of this
branch conflict with base work? A raw line-diff cannot answer that — it
conflates the branch's own harmless staleness with the base's entirely
unrelated, untouched progress, which a real 3-way merge carries through
unchanged. A conflict only exists where both sides genuinely disagree about the
same content (the base deleted or rewrote something the branch also touched) —
exactly the "this branch predates a refactor" shape the remedy needs to name,
and git's own merge simulation is what tells the difference.

Both probes fail CLOSED: any git error reports "still owes" / "no risk measured",
because a false discharge destroys the only record of unshipped work (the same
data-loss doctrine the worktree reapers hold).
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from teatree.utils import git
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

#: Matches one conflicted-path record in ``git merge-tree --write-tree`` output:
#: ``<mode> <blob-sha> <stage 1|2|3>\t<path>``. Every conflicting path emits at
#: least one such line regardless of conflict kind (content, modify/delete,
#: rename), so this is the stable part of the format to key off — conflict
#: *messages* are prose that varies by kind and git version.
_CONFLICT_PATH_RE = re.compile(r"^[0-7]{6} [0-9a-f]{40} [1-3]\t(.+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class RevertRisk:
    """Whether a REAL merge of this branch into the current base would conflict.

    ``measured`` separates a confirmed-clean merge from one that could not be
    simulated — only an actual ``git merge-tree`` run may ever set
    :attr:`at_risk`.
    """

    conflicted_paths: tuple[str, ...] = field(default_factory=tuple)
    measured: bool = False

    @property
    def at_risk(self) -> bool:
        return self.measured and bool(self.conflicted_paths)


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


def _new_occurrences(paths: dict[str, str], forked_from: dict[str, str]) -> Counter[str]:
    """Count, per blob, how many paths in ``paths`` hold it that ``forked_from`` did not.

    Reused for both the branch's tip and the base, so the compare is apples to
    apples: a blob that already sat somewhere in the repo before the branch
    existed (an old, unrelated file that happens to share bytes with new work)
    never counts as "introduced" on either side, no matter how many copies of
    it a change adds or how many pre-existing copies the base already had.
    """
    return Counter(blob for path, blob in paths.items() if forked_from.get(path) != blob)


def branch_content_landed_on_base(repo: str, branch: str, target: str) -> bool:
    """Whether every change ``branch`` carries is already on ``target``, at ANY path.

    Two halves, both judged against the branch's merge-base so only the branch's
    own contribution is weighed:

    - what it INTRODUCES is matched by blob id, counting NEW occurrences only
        (paths that did not already hold that blob at the fork point) — so a
        fix the base took under a different path (the refactor case) counts as
        landed, but a blob that merely pre-existed elsewhere in the repo before
        the branch was created does not, however many copies land on either
        side. The base must have GAINED at least as many fresh occurrences of
        each blob as the branch introduced, not merely already contain it once
        by coincidence;
    - what it REMOVES is matched by ``(path, blob)``, because a removal has only
        landed when that exact content is no longer at that exact path. Blob-only
        matching would read a branch whose entire work is a deletion as landed —
        it introduces nothing — and discharge an obligation that is still real.
        Landed means the path is ABSENT from the base tree, not merely holding
        different content: a base that independently REWROTE the path still has
        it, so the branch's deletion has not actually landed there and would
        still conflict — fail closed rather than silently discharge it.

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

    tip_introduced = _new_occurrences(tip, forked_from)
    base_introduced = _new_occurrences(base, forked_from)
    withdrawn = {(path, blob) for path, blob in forked_from.items() if tip.get(path) != blob}
    if not tip_introduced and not withdrawn:
        return False
    if any(base_introduced[blob] < count for blob, count in tip_introduced.items()):
        return False
    return all(path not in base for path, _blob in withdrawn)


def assess_revert_risk(repo: str, branch: str, target: str) -> RevertRisk:
    """Simulate merging ``branch`` into ``target`` and report any CONFLICTING paths.

    A real ``git merge-tree`` 3-way merge — not a line-diff — is the correct
    signal: an ordinary branch that is merely behind an actively-developed base
    merges through CLEAN (the base's unrelated progress is untouched by a real
    merge, so nothing about it is ever "removed"). A conflict only arises where
    the base changed or deleted something the branch also touched since the
    fork — exactly the shape of a branch stranded behind a refactor, and
    nothing else trips it.

    Fails CLOSED: an unresolvable ref, or any git invocation error, reports
    unmeasured — never a false "at risk", and never a false "clean".
    """
    try:
        git.run_strict(repo=repo, args=["rev-parse", "--verify", "--quiet", f"{target}^{{commit}}"])
        git.run_strict(repo=repo, args=["rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"])
    except CommandFailedError:
        return RevertRisk()
    try:
        result = run_allowed_to_fail(
            ["git", "-C", repo, "merge-tree", "--write-tree", target, branch],
            expected_codes={0, 1},
        )
    except CommandFailedError:
        return RevertRisk()
    if result.returncode == 0:
        return RevertRisk(measured=True)
    paths = tuple(sorted({match.group(1) for match in _CONFLICT_PATH_RE.finditer(result.stdout)}))
    return RevertRisk(conflicted_paths=paths, measured=True)


__all__ = ["RevertRisk", "assess_revert_risk", "branch_content_landed_on_base"]
