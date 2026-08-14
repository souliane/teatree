"""The read-only front door: is this branch's work already on the default branch? (#4070).

:mod:`teatree.core.worktree.branch_classification` has answered this correctly for
months, and no surface EXPOSED it. ``workspace landscape`` cannot: its ``has_unpushed``
comes from ``commits_absent_from_all_remotes``, which is sha-based and — per its own
docstring — is not empty for a squash-merge, deliberately fail-OPEN because intake asks
"might something be in flight?", not "is it landed?". ``workspace emit`` signals a landed
branch only by ABSENCE. So an agent asking the question had nothing to run and
hand-rolled ``git cherry origin/main HEAD``, which a squash-merge defeats.

This module serializes :func:`branch_classification.branch_redundancy` and adds ONE
layer of its own — :func:`_content_still_present`, the present-tense check the patch-id
layers structurally cannot make. No FSM write, no forge mutation, no dependence on a
registered ``Worktree`` row, so it answers for any local branch.

:class:`BranchVerdict` carries ``forge_merged``, ``merged_with_post_merge_work`` and
``unique_shas`` together, and that grouping is the point: a caller shown only "the forge
says merged" reads it as "safe to delete", while the shas beside it are the post-merge
delta a fresh PR still owes. Keeping them in one payload makes the expensive misreading
impossible to reach.
"""

from dataclasses import dataclass, field

from teatree.core.worktree.branch_classification import branch_redundancy, effective_default_target
from teatree.utils import git
from teatree.utils.run import CommandFailedError


@dataclass(frozen=True, slots=True)
class BranchVerdict:
    """One branch's landed-ness, with the deciding layer and the delta it still owes."""

    branch: str
    target: str
    redundant: bool
    source: str
    forge_merged: bool
    merged_with_post_merge_work: bool
    content_present_on_target: bool
    unique_shas: list[str] = field(default_factory=list)


def _content_still_present(repo: str, branch: str, target: str) -> bool:
    """Whether merging *branch* into *target* would leave ``target``'s tree unchanged.

    Every layer beneath this one compares patch-IDs, which record a patch's PRIOR
    appearance on the target — and a revert does not erase that, so a squash-merged
    then reverted branch reads as landed and the fresh PR re-landing it is refused.
    This asks the present-tense question instead.

    Fails toward NOT-present: a git error, an unresolvable ref, or a merge CONFLICT
    (``main`` re-edited the same region) is not a proof of presence, and an
    unnecessary PR is the cheap direction to be wrong in.
    """
    try:
        merged = git.run_strict(repo=repo, args=["merge-tree", "--write-tree", target, branch])
        target_tree = git.run_strict(repo=repo, args=["rev-parse", f"{target}^{{tree}}"])
    except CommandFailedError:
        return False
    lines = merged.splitlines()
    return bool(lines) and lines[0].strip() == target_tree


def branch_verdict_report(repo: str, branch: str, target: str = "") -> BranchVerdict:
    """The canonical layered verdict on *branch*, serialized.

    An empty *target* resolves the repo's REAL default branch (``origin/master`` on a
    master-default repo, a pinned branch on a declared single-branch repo) rather than
    assuming ``origin/main`` — measuring against a base the repo does not have makes
    every layer inconclusive, and inconclusive reads as NOT landed.
    """
    resolved = target or effective_default_target(repo)
    verdict = branch_redundancy(repo, branch, resolved)
    return BranchVerdict(
        branch=branch,
        target=resolved,
        redundant=verdict.redundant,
        source=verdict.source,
        forge_merged=verdict.forge_merged,
        merged_with_post_merge_work=verdict.merged_with_post_merge_work,
        content_present_on_target=_content_still_present(repo, branch, resolved),
        unique_shas=list(verdict.unique_shas),
    )


def branch_is_landed(repo: str, branch: str) -> bool:
    """Whether *branch*'s CURRENT tip is provably captured on the repo's default branch.

    The boolean view for a caller that only needs the decision — a duplicate-PR refusal,
    a redundancy check. Fail-CLOSED like every layer beneath it: an inconclusive probe
    answers False (not landed), so an uncertain branch is worked on rather than written
    off.

    Presence is checked FIRST and short-circuits. It is the layer that a revert on the
    target defeats every patch-id layer with, and it is pure git — so the ordinary
    unlanded branch opening its first PR pays neither of ``branch_redundancy``'s two
    30s forge probes.
    """
    resolved = effective_default_target(repo)
    return _content_still_present(repo, branch, resolved) and branch_redundancy(repo, branch, resolved).redundant


def render_verdict(verdict: BranchVerdict) -> str:
    """One human line per branch, with the post-merge delta never omitted.

    A landed branch reads as landed; a forge-merged branch that still owes work says so
    on the same line, because that is the pair a reader acts on.
    """
    landed = "LANDED" if verdict.redundant else "NOT LANDED"
    line = f"  {verdict.branch}: {landed} vs {verdict.target} ({verdict.source})"
    if verdict.redundant and not verdict.content_present_on_target:
        line += (
            f"\n    the patch landed once but its content is NOT on {verdict.target} now "
            "(reverted there, or that region re-edited): a fresh PR is still owed"
        )
    if verdict.merged_with_post_merge_work:
        return (
            f"{line}\n    forge says merged, but {len(verdict.unique_shas)} commit(s) are NOT on the target: "
            + ", ".join(sha[:8] for sha in verdict.unique_shas)
        )
    return line
