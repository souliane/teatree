class InvalidTransitionError(ValueError):
    pass


class SessionNotFound(LookupError):  # noqa: N818
    pass


class NoPlanArtifactError(InvalidTransitionError):
    """plan() was attempted without a PlanArtifact record for the ticket."""


class QualityGateError(ValueError):
    pass


class DirtyWorktreeError(InvalidTransitionError):
    """A FSM transition was refused because a worktree has uncommitted tracked changes.

    Owner-resolved policy (#884): the transition does not advance and the
    pending phase task is reopened so the agent commits or discards the
    change first. No auto-stash — worktrees share ``.git`` so a stash is
    repo-global (the foreign-stash hazard, near-miss class #806).
    """
