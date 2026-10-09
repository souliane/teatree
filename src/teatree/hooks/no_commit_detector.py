"""Detect a sub-agent that terminated on a work branch with ZERO commits (#1205).

When an ``isolation: worktree`` sub-agent only edits files and never commits,
the worktree is auto-cleaned on teardown and the edits are lost silently. The
orchestrator then believes work landed when nothing did — a phantom-completion
source. This module is the deterministic detector the ``SubagentStop`` hook in
``hooks/scripts/hook_router.py`` runs once per sub-agent termination so the
no-commit termination is *recorded and surfaced* instead of silently lost.

Pure detection: given a worktree path, it answers one question — did this
sub-agent terminate on a work branch with no new commits relative to its base?
It is a DETECTION/surfacing primitive, not a hard deny; ``SubagentStop`` cannot
un-terminate the agent, so the value is the recorded signal.

Conservative by contract — it flags only a *confirmed* zero on a *confirmed*
work branch, and **fails OPEN** (verdict ``UNDETERMINED``, not flagged) on any
inability to introspect git:

* A detached HEAD (a read-only/review worktree at a SHA) is NOT a work branch.
* A worktree on the repo's base branch (``main`` / ``master`` / ``development``) is not a work branch.
* A branch with at least one commit ahead of its base DID produce work.
* No repo, an undetectable base, or any git error yields ``UNDETERMINED``.

A detection bug must never manufacture a false phantom-completion alarm: every
non-confirmed case is silently not flagged.

Reuses the single git-plumbing seam :mod:`teatree.utils.git` (the same
``rev_count(base..branch)`` introspection
:func:`teatree.core.management.commands._ship.gates.assert_commits_ahead_of_base`
uses for the hollow-ship gate), so the no-commit definition is consistent
across the codebase.
"""

from dataclasses import dataclass
from enum import Enum

from teatree.utils import git
from teatree.utils.run import CommandFailedError

# Branch names that are never a sub-agent *work* branch: a worktree resolved
# to one of these (or to a detached HEAD) is a base checkout or a read-only
# review worktree, not a feature branch that was supposed to accumulate commits.
NON_WORK_BRANCHES = frozenset({"main", "master", "development", "develop", "HEAD"})


class NoCommitVerdict(Enum):
    """Outcome of the no-commit introspection for one sub-agent worktree."""

    TERMINATED_WITHOUT_COMMIT = "terminated_without_commit"
    """Confirmed: a work branch with 0 commits ahead of its base."""

    COMMITTED = "committed"
    """The branch has ≥1 commit ahead of its base — work landed."""

    NOT_A_WORK_BRANCH = "not_a_work_branch"
    """Detached HEAD or a base/default branch — a read-only/review checkout."""

    UNDETERMINED = "undetermined"
    """Git state could not be introspected — fail open, never flag."""


@dataclass(frozen=True, slots=True)
class NoCommitFinding:
    """The verdict plus the context needed to record/surface it."""

    verdict: NoCommitVerdict
    worktree: str
    branch: str = ""
    base: str = ""

    @property
    def is_flagged(self) -> bool:
        """True only for the confirmed ``TERMINATED_WITHOUT_COMMIT`` verdict."""
        return self.verdict is NoCommitVerdict.TERMINATED_WITHOUT_COMMIT


def _resolve_base(worktree: str) -> str | None:
    """``origin/<default>`` for *worktree*, or ``None`` when undetectable.

    Mirrors the ship-gate base resolver. A repo whose default branch cannot be
    determined (no ``origin``, bare path, git error) yields ``None`` so the
    caller fails open rather than comparing against a guessed base.
    """
    try:
        return f"origin/{git.default_branch(repo=worktree)}"
    except (CommandFailedError, RuntimeError, ValueError, OSError):
        return None


def _resolve_work_branch(worktree: str) -> NoCommitFinding | str:
    """Return the work-branch name, or a terminal non-flagged finding.

    A terminal finding short-circuits :func:`detect`: ``UNDETERMINED`` when git
    cannot name a branch (not a repo / swallowed introspection error),
    ``NOT_A_WORK_BRANCH`` for a detached HEAD (``HEAD``) or a base/default
    branch. Otherwise returns the branch name so the caller can count commits.
    """
    try:
        branch = git.current_branch(repo=worktree)
    except (CommandFailedError, RuntimeError, ValueError, OSError):
        return NoCommitFinding(NoCommitVerdict.UNDETERMINED, worktree=worktree)
    if not branch:
        return NoCommitFinding(NoCommitVerdict.UNDETERMINED, worktree=worktree)
    if branch in NON_WORK_BRANCHES:
        return NoCommitFinding(NoCommitVerdict.NOT_A_WORK_BRANCH, worktree=worktree, branch=branch)
    return branch


def detect(worktree: str) -> NoCommitFinding:
    """Decide whether *worktree*'s sub-agent terminated without committing.

    Conservative and fail-open: returns ``TERMINATED_WITHOUT_COMMIT`` only on a
    confirmed zero (``rev_count(base..branch) == 0``) on a confirmed work
    branch. Any read-only/detached checkout is ``NOT_A_WORK_BRANCH``; any commit
    ahead is ``COMMITTED``; any inability to introspect git is ``UNDETERMINED``.
    Never raises — a detection error degrades to ``UNDETERMINED``.
    """
    if not worktree:
        return NoCommitFinding(NoCommitVerdict.UNDETERMINED, worktree=worktree)

    resolved = _resolve_work_branch(worktree)
    if isinstance(resolved, NoCommitFinding):
        return resolved
    branch = resolved

    base = _resolve_base(worktree)
    if base is None:
        return NoCommitFinding(NoCommitVerdict.UNDETERMINED, worktree=worktree, branch=branch)

    try:
        ahead = git.rev_count(repo=worktree, range_spec=f"{base}..{branch}")
    except (CommandFailedError, RuntimeError, ValueError, OSError):
        return NoCommitFinding(NoCommitVerdict.UNDETERMINED, worktree=worktree, branch=branch, base=base)

    verdict = NoCommitVerdict.COMMITTED if ahead > 0 else NoCommitVerdict.TERMINATED_WITHOUT_COMMIT
    return NoCommitFinding(verdict, worktree=worktree, branch=branch, base=base)
