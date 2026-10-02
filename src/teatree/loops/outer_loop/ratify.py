"""The RATIFY phase — the ONLY writer of the ADMITTED state (T4-PR-3).

Ratification is structural, not advisory: :func:`ask_ratification` records a
:class:`~teatree.core.models.deferred_question.DeferredQuestion` and moves the
experiment to ``RATIFY_PENDING``; :func:`try_admit` is the sole path that calls
:meth:`OuterLoopExperiment.admit` — and only after a human's recorded answer
approves it. A denial rejects; an answer that decides neither is re-asked. There is
no auto-admit code path anywhere, so an experiment cannot become ``ADMITTED``
without a consumed question.
"""

from teatree.core.models import DeferredQuestion, OuterLoopExperiment
from teatree.core.models.ratification import RatificationVerdict, classify_ratification_answer

#: How much of the undecidable answer to quote back, bounded so a long one cannot bloat the DM.
_EXCERPT_LEN = 500


def ask_ratification(experiment: OuterLoopExperiment) -> DeferredQuestion:
    """Record the ratify question and move the experiment to ``RATIFY_PENDING``."""
    question = DeferredQuestion.record(
        f"Ratify outer-loop experiment: {experiment.hypothesis} "
        f"(target {experiment.target_provider_id}). Approve to admit?",
        options_hash=f"outer_loop_ratify:{experiment.pk}",
    )
    experiment.attach_ratification(question)
    return question


def try_admit(experiment: OuterLoopExperiment) -> str:
    """Resolve a ``RATIFY_PENDING`` experiment from its answered question.

    Returns ``"admitted"`` (approved), ``"rejected"`` (denied), ``"reasked"`` (the
    answer decided nothing, so a fresh question replaces it and the experiment holds),
    or ``"pending"`` (no answer yet). The single ``admit()`` call site.
    """
    question = experiment.ratify_question
    if question is None or question.answered_at is None:
        return "pending"
    verdict = classify_ratification_answer(question.answer_text)
    if verdict is RatificationVerdict.APPROVAL:
        experiment.admit()
        return "admitted"
    if verdict is RatificationVerdict.DENIAL:
        experiment.reject(f"ratification denied: {question.answer_text.strip()!r}")
        return "rejected"
    experiment.reask_ratification(_undecidable_answer_question(experiment, question))
    return "reasked"


def _undecidable_answer_question(experiment: OuterLoopExperiment, answered: DeferredQuestion) -> DeferredQuestion:
    """Re-ask the ratify question, quoting back the answer that decided nothing."""
    return DeferredQuestion.record(
        f"Outer-loop experiment {experiment.pk} is STILL awaiting ratification — the recorded "
        f"answer read as neither an approval nor a denial, so nothing was decided and the "
        f"experiment was held.\n\nPrevious answer: {answered.answer_text.strip()[:_EXCERPT_LEN]!r}\n\n"
        f"Hypothesis: {experiment.hypothesis} (target {experiment.target_provider_id})\n\n"
        f"Answer 'approve' to admit, or 'reject' to deny.",
        options_hash=f"outer_loop_ratify:{experiment.pk}:reask",
    )
