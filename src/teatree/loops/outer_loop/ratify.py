"""The RATIFY phase — the ONLY writer of the ADMITTED state (T4-PR-3).

Ratification is structural, not advisory: :func:`ask_ratification` records a
:class:`~teatree.core.models.deferred_question.DeferredQuestion` and moves the
experiment to ``RATIFY_PENDING``; :func:`try_admit` is the sole path that calls
:meth:`OuterLoopExperiment.admit` — and only after a human's recorded answer
approves it. A denial rejects. There is no auto-admit code path anywhere, so an
experiment cannot become ``ADMITTED`` without a consumed question.
"""

from teatree.core.models import DeferredQuestion, OuterLoopExperiment

_APPROVE_TOKENS = frozenset({"approve", "approved", "yes", "y", "1", "ratify", "admit", "ok"})


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

    Returns ``"admitted"`` (approved), ``"rejected"`` (denied), or ``"pending"``
    (no answer yet). The single ``admit()`` call site.
    """
    question = experiment.ratify_question
    if question is None or question.answered_at is None:
        return "pending"
    if _is_approval(question.answer_text):
        experiment.admit()
        return "admitted"
    experiment.reject(f"ratification denied: {question.answer_text.strip()!r}")
    return "rejected"


def _is_approval(answer: str) -> bool:
    return answer.strip().lower() in _APPROVE_TOKENS
