"""The REVERT flow — human-ratified rollback of a non-improving experiment (T4-PR-3).

Mirrors :mod:`teatree.loops.outer_loop.ratify`: :func:`ask_revert` records the
revert :class:`~teatree.core.models.deferred_question.DeferredQuestion` (so a
``REVERT_PENDING`` experiment visibly asks a human, rather than dead-ending and
holding the max-concurrent slot forever); :func:`resolve_revert` is the operator's
close-out — it consumes that question and drives the experiment to the terminal
``REVERTED`` state, freeing the slot for the next experiment.

The human performs the actual git revert of the merged change; the loop only
drives the ask and the terminal state. Full AUTO-revert is a later PR — this keeps
the human in the loop while removing the soft-lock.
"""

from teatree.core.models import DeferredQuestion, OuterLoopExperiment


def ask_revert(experiment: OuterLoopExperiment) -> DeferredQuestion:
    """Record the revert question and bind it (the experiment must be ``REVERT_PENDING``)."""
    question = DeferredQuestion.record(
        f"Outer-loop experiment #{experiment.pk} did not improve "
        f"{experiment.target_provider_id}: {experiment.decision_reason}. "
        f"Perform the git revert and close it via `t3 outer resolve-revert {experiment.pk}`.",
        options_hash=f"outer_loop_revert:{experiment.pk}",
    )
    experiment.attach_revert_question(question)
    return question


def resolve_revert(experiment: OuterLoopExperiment, *, revert_sha: str = "") -> None:
    """Close a ``REVERT_PENDING`` experiment to terminal ``REVERTED``, freeing the slot.

    Ensures a revert question exists (asking one if the tick has not yet), consumes
    it, then records the revert with the operator-supplied revert commit sha.
    """
    question = experiment.revert_question or ask_revert(experiment)
    if question.answered_at is None:
        DeferredQuestion.consume(question.pk, answer="reverted")
        experiment.revert_question = DeferredQuestion.objects.get(pk=question.pk)
    experiment.record_reverted(revert_sha=revert_sha)
