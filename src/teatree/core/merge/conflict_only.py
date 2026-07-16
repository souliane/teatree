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
committed merge tree deviates from that auto-merge tree is in git's OWN
authoritative conflicted-path set for that auto-merge. Any deviation on a
cleanly-merged path is a substantive change — even when that file's content
legitimately contains literal conflict-marker strings (a doc/fixture), which a
marker-grep would misread as "was conflicted" and fail OPEN.

Every uncertainty fails SAFE — a non-two-parent commit or any git error returns
``False`` (force re-review), never a false "conflict-only" that would skip an
independent review.
"""

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.models.merge_clear import is_commit_sha
from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict, ReviewVerdictError
from teatree.utils.run import run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.models import MergeClear
    from teatree.utils.run import CompletedProcess

logger = logging.getLogger(__name__)

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


def _auto_merge(repo_root: str, p1: str, p2: str) -> "tuple[str, frozenset[str]] | None":
    """Git's machine auto-merge of two parents: (merged tree OID, conflicted-path set).

    ``git merge-tree --write-tree --name-only -z`` emits the merged tree OID
    followed by git's OWN authoritative list of the paths it could not cleanly
    auto-merge — NUL-separated, terminated by an empty record before the
    informational-message section. A path is conflicted iff git reports it here,
    never inferred from marker strings a cleanly-merged blob may legitimately
    carry. Returns ``None`` on any git error / unparsable OID (fails safe).
    """
    result = _git(repo_root, ["merge-tree", "--write-tree", "--name-only", "-z", p1, p2])
    records = result.stdout.split("\x00")
    if not _looks_like_oid(records[0]):
        return None
    conflicted: set[str] = set()
    for record in records[1:]:
        if not record:
            break
        conflicted.add(record)
    return records[0].strip(), frozenset(conflicted)


def _resolve_default_branch(repo_root: str) -> str:
    """The repo's default branch name (``origin/HEAD`` target), falling back to ``main``."""
    result = _git(repo_root, ["rev-parse", "--abbrev-ref", "origin/HEAD"])
    ref = result.stdout.strip()
    if result.returncode == 0 and "/" in ref:
        return ref.split("/", 1)[1]
    return "main"


def _fresh_base_ref(repo_root: str, base_branch: str) -> "str | None":
    """The base ref ``parents[1]`` must be an ancestor of, or ``None`` when unverifiable.

    When an ``origin`` remote exists, fetch ``base_branch`` FRESH and return
    ``origin/<base>`` so a moved base is current (a stale local copy can never
    launder an attacker's second parent past the ancestry check). Refuse
    (``None``) if the fresh fetch fails or the fetched ref does not resolve — a
    base we cannot re-read fresh is unverifiable, so the rebind fails safe. For
    an origin-less clone (no forge to verify against) fall back to the local
    branch; ``None`` when even that does not resolve.
    """
    has_origin = _git(repo_root, ["remote", "get-url", "origin"]).returncode == 0
    if has_origin:
        if _git(repo_root, ["fetch", "--quiet", "origin", base_branch]).returncode != 0:
            return None
        remote_ref = f"origin/{base_branch}"
        if _git(repo_root, ["rev-parse", "--verify", "--quiet", f"{remote_ref}^{{commit}}"]).returncode == 0:
            return remote_ref
        return None
    if _git(repo_root, ["rev-parse", "--verify", "--quiet", f"{base_branch}^{{commit}}"]).returncode == 0:
        return base_branch
    return None


def _second_parent_is_trusted_base(repo_root: str, second_parent: str, base_branch: str) -> bool:
    """True iff ``second_parent`` is a forge-verified ancestor of the PR base (§17.4).

    The conflict-resolution merge git rebinds a clearance across is ``merge
    origin/<base> INTO <reviewed-branch>`` — so its SECOND parent must be a commit
    reachable from the PR's base branch. An attacker who instead merges an ARBITRARY
    unreviewed branch (even one that auto-merges cleanly, so the deviation set is
    empty and :func:`is_conflict_only_merge_commit` returns ``True``) would otherwise
    carry the original reviewer's verdict forward onto code that base never saw. This
    check refuses that: ``second_parent`` must be an ancestor of the FRESH base ref
    (:func:`_fresh_base_ref`). Any uncertainty (base unresolvable, ancestry check
    errors) fails SAFE — force a fresh review.
    """
    base = base_branch.strip() or _resolve_default_branch(repo_root)
    if not base:
        return False
    base_ref = _fresh_base_ref(repo_root, base)
    if base_ref is None:
        return False
    return _git(repo_root, ["merge-base", "--is-ancestor", second_parent, base_ref]).returncode == 0


def is_conflict_only_merge_commit(repo_root: str, merge_sha: str) -> bool:
    r"""True iff ``merge_sha`` is a two-parent merge that only resolves conflicts.

    Compares the committed merge tree against ``git merge-tree --write-tree`` of
    its two parents: conflict-only iff every deviating path is in git's
    authoritative conflicted-path set for that auto-merge. An empty deviation set
    (the commit is exactly the machine merge) is trivially conflict-only.

    Both sides read ``-z`` (NUL-separated, verbatim): without it the deviation
    diff C-quotes a non-ASCII path (``café.py`` → ``"caf\303\251.py"``) under
    ``core.quotePath`` while the conflicted-path set stays verbatim, so the two
    never match — a real conflict-only merge over-blocks and a decoy path crafted
    to collide under C-quoting could fail OPEN.
    """
    parents = merge_commit_parents(repo_root, merge_sha)
    if len(parents) != _MERGE_PARENT_COUNT:
        return False
    p1, p2 = parents
    auto = _auto_merge(repo_root, p1, p2)
    if auto is None:
        return False
    auto_tree, conflicted_paths = auto
    merge_tree = _git(repo_root, ["rev-parse", f"{merge_sha}^{{tree}}"]).stdout.strip()
    if not _looks_like_oid(merge_tree):
        return False
    diff = _git(repo_root, ["diff", "--name-only", "-z", auto_tree, merge_tree])
    if diff.returncode != 0:
        return False
    deviations = [record for record in diff.stdout.split("\x00") if record]
    return all(path in conflicted_paths for path in deviations)


def _merge_is_trusted_conflict_only(repo_root: str, merge: str, reviewed: str, base_branch: str) -> bool:
    """True iff ``merge`` is a conflict-only merge of the reviewed tree with a trusted base.

    ALL must hold: the merge commit's FIRST parent is exactly ``reviewed`` (the
    reviewed tree origin/base was merged INTO); its SECOND parent is a forge-verified
    ancestor of the PR base (:func:`_second_parent_is_trusted_base` — never an
    arbitrary unreviewed branch); and it is conflict-resolution-only.
    """
    parents = merge_commit_parents(repo_root, merge)
    if len(parents) != _MERGE_PARENT_COUNT or parents[0].strip().lower() != reviewed:
        return False
    if not _second_parent_is_trusted_base(repo_root, parents[1], base_branch):
        return False
    return is_conflict_only_merge_commit(repo_root, merge)


def _carry_forward_candidates(
    *, clear: "MergeClear", merge: str, repo_root: str, base_branch: str
) -> "list[ReviewVerdict] | None":
    """The merge_safe verdicts to carry forward, or ``None`` when re-bind is refused.

    Returns ``None`` (no re-bind) unless BOTH hold:
    (a) :func:`_merge_is_trusted_conflict_only` (first parent is the reviewed SHA,
    second parent is a trusted base, and the merge is conflict-resolution-only), and
    (b) at least one independent ``merge_safe`` verdict vouches for the reviewed tree
    and no unresolved HOLD supersedes it.
    """
    reviewed = str(getattr(clear, "reviewed_sha", "") or "").strip().lower()
    if not is_commit_sha(reviewed) or not is_commit_sha(merge):
        return None
    if not _merge_is_trusted_conflict_only(repo_root, merge, reviewed, base_branch):
        return None

    verdicts_at_reviewed = list(ReviewVerdict.objects.filter(pr_id=clear.pr_id, reviewed_sha=reviewed))
    merge_safe = [verdict for verdict in verdicts_at_reviewed if verdict.is_merge_safe()]
    if not merge_safe:
        return None
    # An unresolved HOLD at the reviewed tree must not be shed by the re-bind:
    # if any slug's effective verdict is a HOLD, refuse (a fresh review is owed).
    for slug in {verdict.slug for verdict in verdicts_at_reviewed}:
        state = ReviewVerdict.objects.effective_state_at(slug=slug, pr_id=clear.pr_id, head_sha=reviewed)
        if state is HeadVerdictState.HOLD:
            return None
    return merge_safe


def rebind_clearance_after_conflict_only_merge(
    *,
    clear: "MergeClear",
    merge_sha: str,
    repo_root: str,
    base_branch: str = "",
) -> bool:
    """Re-bind an existing clearance to a conflict-only merge commit (no re-review).

    Returns ``True`` and re-binds iff ALL hold: (a) the merge commit's FIRST
    parent is exactly the SHA the clearance was reviewed at (the reviewed tree is
    literally the branch tip origin/main was merged INTO), (b) the merge commit's
    SECOND parent is a forge-verified ancestor of the PR base branch (``base_branch``,
    default the repo's resolved default branch — the merged-in side must be base, not
    an arbitrary unreviewed branch), (c) the merge commit is conflict-resolution-only,
    (d) an independent ``merge_safe`` verdict vouches for the reviewed tree and no
    unresolved HOLD supersedes it. On re-bind, every
    such verdict is carried forward to the merge SHA via
    :meth:`ReviewVerdict.carry_forward` (fresh rows keeping the ORIGINAL
    independent reviewer identity — NOT a new self-review — and preserving the
    human-authorized expedite waiver of a PENDING-checks CLEAR) and
    ``clear.reviewed_sha`` advances to the merge SHA, so the standard merge
    preconditions pass at the new head. Any failing condition returns ``False``
    and changes nothing — the SHA-bind gate keeps refusing the moved head, forcing
    a fresh review. A genuinely-unwaivable carry-forward
    (:class:`ReviewVerdictError`) is caught and refused CLEANLY (``False`` +
    atomic rollback), never a CLI traceback.
    """
    merge = merge_sha.strip().lower()
    merge_safe = _carry_forward_candidates(clear=clear, merge=merge, repo_root=repo_root, base_branch=base_branch)
    if merge_safe is None:
        return False

    try:
        with transaction.atomic():
            for verdict in merge_safe:
                verdict.carry_forward(reviewed_sha=merge)
            clear.reviewed_sha = merge
            clear.save(update_fields=["reviewed_sha"])
    except ReviewVerdictError:
        logger.warning(
            "conflict-only rebind refused for CLEAR %s at %s: a merge_safe verdict could not be "
            "carried forward; a fresh review is required",
            getattr(clear, "pk", "?"),
            merge[:8],
        )
        return False
    return True
