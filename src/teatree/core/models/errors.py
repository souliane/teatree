class InvalidTransitionError(ValueError):
    pass


class LeaseLostError(InvalidTransitionError):
    """A heartbeat renewal found the task no longer claimed by this worker.

    Raised by ``Task.renew_lease`` when its compare-and-swap matches zero rows:
    the claim generation moved on (the lease expired and another worker
    reclaimed the task, or the row went terminal). The heartbeating worker must
    abort rather than re-stamp a lease it no longer owns — re-stamping would
    resurrect a dead claim and let two workers drive the same unit.
    """


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
    """mark_delivered() was refused: a BLOCKING deterministic rubric item failed and enforcement is live.

    Raised by ``critic_gate.check_critic`` (SELFCATCH-5) ONLY when ``critic_gate_mode``
    is ``blocking`` for the ticket's overlay AND a deterministic BLOCKING item
    (done_not_done / spec_not_plan / completeness) failed. The async LLM items are
    advisory and never raise. In the default ``off`` (and the ``advisory``) posture the
    critic records findings and this is never raised — the delivery proceeds.

    Carries the computed ``specs`` so the caller can re-record them OUTSIDE the delivery
    ``transaction.atomic()`` after it rolls back — otherwise the block would erase the very
    findings its message tells the operator to resolve (the enforcing-mode rollback bug).
    """

    def __init__(self, message: str, *, specs: "list | None" = None) -> None:
        super().__init__(message)
        self.specs = specs or []


class QualityGateError(ValueError):
    pass


class DirtyWorktreeError(InvalidTransitionError):
    """A FSM transition was refused because a worktree has uncommitted tracked changes.

    Owner-resolved policy (#884): the transition does not advance and the
    pending phase task is reopened so the agent commits or discards the
    change first. No auto-stash — worktrees share ``.git`` so a stash is
    repo-global (the foreign-stash hazard, near-miss class #806).
    """
