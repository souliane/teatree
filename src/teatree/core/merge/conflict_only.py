"""Deterministic conflict-only merge-commit detection + clearance re-bind (§17.4).

When a reviewed PR branch has ``origin/main`` merged into it to resolve conflicts
(merge, NEVER rebase — §17.4), the branch head moves to the new merge commit,
which the SHA-bind gate would refuse as a moved head — forcing a re-review. This
module decides, deterministically, whether that merge commit is
CONFLICT-RESOLUTION-ONLY (introducing nothing beyond reconciling the two parents)
and, when it is, re-binds the existing independent review clearance to the merge
commit so no re-review is required. A SUBSTANTIVE merge commit (an "evil merge"
that also changes a cleanly-auto-merged file) is NOT re-bound: the SHA-bind gate
keeps refusing it and a fresh review is forced.

The oracle is ``git merge-tree --write-tree`` — git's own machine auto-merge of
the two parents. The merge commit is conflict-only iff every path where the
committed merge tree deviates from that auto-merge tree was a CONFLICTED path in
the auto-merge (its auto-merged blob carries git conflict markers). Any deviation
on a cleanly-merged path is a substantive change.

Every uncertainty fails SAFE — a non-two-parent commit, a git error, or an
unreadable blob returns ``False`` (force re-review), never a false "conflict-only"
that would skip an independent review.
"""

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.models.merge_clear import is_commit_sha
from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict
from teatree.utils.run import run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.models import MergeClear
    from teatree.utils.run import CompletedProcess

logger = logging.getLogger(__name__)

_CONFLICT_START = "<<<<<<<"
_CONFLICT_END = ">>>>>>>"
_OID_ALPHABET = frozenset("0123456789abcdef")
_MIN_OID_LEN = 40
_MERGE_PARENT_COUNT = 2


def _git(repo_root: str, args: list[str]) -> "CompletedProcess[str]":
    return run_allowed_to_fail(["git", "-C", repo_root, *args], expected_codes=None)


def _looks_like_oid(value: str) -> bool:
    candidate = value.strip().lower()
    return len(candidate) >= _MIN_OID_LEN and all(char in _OID_ALPHABET for char in candidate)


def merge_commit_parents(repo_root: str, merge_sha: str) -> tuple[str, ...]:
    """The parent SHAs of ``merge_sha`` (empty tuple on error / unknown commit)."""
    result = _git(repo_root, ["rev-list", "--parents", "-n", "1", merge_sha])
    if result.returncode != 0 or not result.stdout.strip():
        return ()
    # ``<commit> <parent1> <parent2> ...`` — drop the commit itself.
    return tuple(result.stdout.strip().split()[1:])


def _path_was_conflicted(repo_root: str, tree: str, path: str) -> bool:
    """True iff the auto-merge blob at ``path`` in ``tree`` carries conflict markers.

    Fails safe: an unreadable path (add/add or delete conflicts leave no single
    blob) returns ``False`` so it counts as a substantive deviation, never a
    silently-allowed one.
    """
    result = _git(repo_root, ["cat-file", "-p", f"{tree}:{path}"])
    if result.returncode != 0:
        return False
    return _CONFLICT_START in result.stdout and _CONFLICT_END in result.stdout


def is_conflict_only_merge_commit(repo_root: str, merge_sha: str) -> bool:
    """True iff ``merge_sha`` is a two-parent merge that only resolves conflicts.

    Compares the committed merge tree against ``git merge-tree --write-tree`` of
    its two parents: conflict-only iff every deviating path was conflicted in the
    machine auto-merge. An empty deviation set (the commit is exactly the machine
    merge) is trivially conflict-only.
    """
    parents = merge_commit_parents(repo_root, merge_sha)
    if len(parents) != _MERGE_PARENT_COUNT:
        return False
    p1, p2 = parents
    auto = _git(repo_root, ["merge-tree", "--write-tree", p1, p2])
    auto_tree = auto.stdout.splitlines()[0].strip() if auto.stdout.strip() else ""
    if not _looks_like_oid(auto_tree):
        return False
    merge_tree = _git(repo_root, ["rev-parse", f"{merge_sha}^{{tree}}"]).stdout.strip()
    if not _looks_like_oid(merge_tree):
        return False
    diff = _git(repo_root, ["diff", "--name-only", auto_tree, merge_tree])
    if diff.returncode != 0:
        return False
    deviations = [line for line in diff.stdout.splitlines() if line.strip()]
    return all(_path_was_conflicted(repo_root, auto_tree, path) for path in deviations)


def rebind_clearance_after_conflict_only_merge(
    *,
    clear: "MergeClear",
    merge_sha: str,
    repo_root: str,
) -> bool:
    """Re-bind an existing clearance to a conflict-only merge commit (no re-review).

    Returns ``True`` and re-binds iff ALL hold: (a) the merge commit's FIRST
    parent is exactly the SHA the clearance was reviewed at (the reviewed tree is
    literally the branch tip origin/main was merged INTO), (b) the merge commit is
    conflict-resolution-only, (c) an independent ``merge_safe`` verdict vouches
    for the reviewed tree and no unresolved HOLD supersedes it. On re-bind, every
    such verdict is carried forward to the merge SHA (fresh rows keeping the
    ORIGINAL independent reviewer identity — NOT a new self-review) and
    ``clear.reviewed_sha`` advances to the merge SHA, so the standard merge
    preconditions pass at the new head. Any failing condition returns ``False``
    and changes nothing — the SHA-bind gate keeps refusing the moved head, forcing
    a fresh review.
    """
    reviewed = str(getattr(clear, "reviewed_sha", "") or "").strip().lower()
    merge = merge_sha.strip().lower()
    if not is_commit_sha(reviewed) or not is_commit_sha(merge):
        return False

    parents = merge_commit_parents(repo_root, merge)
    if len(parents) != _MERGE_PARENT_COUNT or parents[0].strip().lower() != reviewed:
        return False
    if not is_conflict_only_merge_commit(repo_root, merge):
        return False

    verdicts_at_reviewed = list(ReviewVerdict.objects.filter(pr_id=clear.pr_id, reviewed_sha=reviewed))
    merge_safe = [verdict for verdict in verdicts_at_reviewed if verdict.is_merge_safe()]
    if not merge_safe:
        return False
    # An unresolved HOLD at the reviewed tree must not be shed by the re-bind:
    # if any slug's effective verdict is a HOLD, refuse (a fresh review is owed).
    for slug in {verdict.slug for verdict in verdicts_at_reviewed}:
        state = ReviewVerdict.objects.effective_state_at(slug=slug, pr_id=clear.pr_id, head_sha=reviewed)
        if state is HeadVerdictState.HOLD:
            return False

    with transaction.atomic():
        for verdict in merge_safe:
            ReviewVerdict.record(
                pr_id=verdict.pr_id,
                slug=verdict.slug,
                reviewed_sha=merge,
                verdict=ReviewVerdict.Verdict.MERGE_SAFE,
                reviewer_identity=verdict.reviewer_identity,
                blast_class=verdict.blast_class,
                gh_verify_result=verdict.gh_verify_result,
                ticket=verdict.ticket,
            )
        clear.reviewed_sha = merge
        clear.save(update_fields=["reviewed_sha"])
    return True
