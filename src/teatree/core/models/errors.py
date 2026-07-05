class InvalidTransitionError(ValueError):
    pass


class NoPlanArtifactError(InvalidTransitionError):
    """plan() was attempted without a PlanArtifact record for the ticket."""


class NoCurrentPlanError(InvalidTransitionError):
    """code()/schedule_coding was attempted with no adequate, current-HEAD-bound plan.

    Raised by ``plan_currency_gate.check_plan_current`` (SELFCATCH-3) when
    ``require_plan_adequacy`` is on and the latest plan is inadequate (a thin/legacy
    row) or STALE — its base_sha moved off the live target HEAD and an intervening
    commit touched a declared integration seam. The message names the
    ``plan-reaffirm`` remediation so the block is never a hard lock.
    """


class CriticGateError(InvalidTransitionError):
    """mark_delivered() was refused because the critic recorded a FAIL and enforcement is live.

    Raised by ``critic_gate.check_critic`` (SELFCATCH-5) ONLY when ``critic_gate_live``
    is on for the ticket's overlay and at least one rubric item failed its predicate.
    In the default ADVISORY posture the critic records the findings and this is never
    raised — the delivery proceeds. The message names the failing rubric items plus
    the kill-switch escape so the block is never a hard lock.
    """


class QualityGateError(ValueError):
    pass


class DirtyWorktreeError(InvalidTransitionError):
    """A FSM transition was refused because a worktree has uncommitted tracked changes.

    Owner-resolved policy (#884): the transition does not advance and the
    pending phase task is reopened so the agent commits or discards the
    change first. No auto-stash — worktrees share ``.git`` so a stash is
    repo-global (the foreign-stash hazard, near-miss class #806).
    """
