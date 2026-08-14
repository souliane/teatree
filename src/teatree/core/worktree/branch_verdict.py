"""The read-only front door: is this branch's work already on the default branch? (#4070).

:mod:`teatree.core.worktree.branch_classification` has answered this correctly for
months, and no surface EXPOSED it. ``workspace landscape`` cannot: its ``has_unpushed``
comes from ``commits_absent_from_all_remotes``, which is sha-based and — per its own
docstring — is not empty for a squash-merge, deliberately fail-OPEN because intake asks
"might something be in flight?", not "is it landed?". ``workspace emit`` signals a landed
branch only by ABSENCE. So an agent asking the question had nothing to run and
hand-rolled ``git cherry origin/main HEAD``, which a squash-merge defeats.

This module is a pure serialization of :func:`branch_classification.branch_redundancy`
— no FSM write, no forge mutation, no dependence on a registered ``Worktree`` row, so it
answers for any local branch.

:class:`BranchVerdict` carries ``forge_merged``, ``merged_with_post_merge_work`` and
``unique_shas`` together, and that grouping is the point: a caller shown only "the forge
says merged" reads it as "safe to delete", while the shas beside it are the post-merge
delta a fresh PR still owes. Keeping them in one payload makes the expensive misreading
impossible to reach.
"""

from dataclasses import dataclass, field

from teatree.core.worktree.branch_classification import branch_redundancy, effective_default_target


@dataclass(frozen=True, slots=True)
class BranchVerdict:
    """One branch's landed-ness, with the deciding layer and the delta it still owes."""

    branch: str
    target: str
    redundant: bool
    source: str
    forge_merged: bool
    merged_with_post_merge_work: bool
    unique_shas: list[str] = field(default_factory=list)


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
        unique_shas=list(verdict.unique_shas),
    )


def branch_is_landed(repo: str, branch: str) -> bool:
    """Whether *branch*'s CURRENT tip is provably captured on the repo's default branch.

    The boolean view for a caller that only needs the decision — a duplicate-PR refusal,
    a redundancy check. Fail-CLOSED like every layer beneath it: an inconclusive probe
    answers False (not landed), so an uncertain branch is worked on rather than written
    off.
    """
    return branch_verdict_report(repo, branch).redundant


def render_verdict(verdict: BranchVerdict) -> str:
    """One human line per branch, with the post-merge delta never omitted.

    A landed branch reads as landed; a forge-merged branch that still owes work says so
    on the same line, because that is the pair a reader acts on.
    """
    landed = "LANDED" if verdict.redundant else "NOT LANDED"
    line = f"  {verdict.branch}: {landed} vs {verdict.target} ({verdict.source})"
    if verdict.merged_with_post_merge_work:
        return (
            f"{line}\n    forge says merged, but {len(verdict.unique_shas)} commit(s) are NOT on the target: "
            + ", ".join(sha[:8] for sha in verdict.unique_shas)
        )
    return line
