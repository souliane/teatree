"""The KEEP flow — retention of an improving experiment (H1-KEEP).

Mirrors :mod:`teatree.loops.outer_loop.revert`:
:func:`~teatree.loops.outer_loop.measure.measure_and_decide` parks an improving experiment
in ``KEEP_PENDING``, :func:`ask_keep` records the keep
:class:`~teatree.core.models.deferred_question.DeferredQuestion`, and :func:`resolve_keep`
drives the experiment to the terminal ``KEPT`` state, freeing the max-concurrent slot.

The owner's standing answer is "kept" (#5096): the question is internal and answered by
policy with an audit row, so :meth:`OuterLoopExperiment.record_kept`'s consumed-question
guard still holds without asking anyone.
"""

from teatree.core.models import DeferredQuestion, OuterLoopExperiment
from teatree.core.models.approval_dial import auto_answer_by_policy


def ask_keep(experiment: OuterLoopExperiment) -> DeferredQuestion:
    """Record the keep question, bind it, and answer it "kept" by policy."""
    question = DeferredQuestion.record(
        f"Outer-loop experiment #{experiment.pk} improved {experiment.target_provider_id}: "
        f"{experiment.decision_reason}. Approve to keep it via `t3 outer resolve-keep {experiment.pk}`.",
        options_hash=f"outer_loop_keep:{experiment.pk}",
    )
    experiment.attach_keep_question(question)
    auto_answer_by_policy(question, "kept")
    experiment.keep_question = DeferredQuestion.objects.get(pk=question.pk)
    return question


def resolve_keep(experiment: OuterLoopExperiment) -> None:
    """Close a ``KEEP_PENDING`` experiment to terminal ``KEPT``, freeing the slot.

    Ensures a keep question exists (asking one if the tick has not yet), consumes it,
    then records the keep.
    """
    question = experiment.keep_question or ask_keep(experiment)
    if question.answered_at is None:
        DeferredQuestion.consume(question.pk, answer="kept")
        experiment.keep_question = DeferredQuestion.objects.get(pk=question.pk)
    experiment.record_kept()
