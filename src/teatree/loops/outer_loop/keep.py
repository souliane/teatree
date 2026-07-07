"""The KEEP flow — human-ratified retention of an improving experiment (H1-KEEP).

Mirrors :mod:`teatree.loops.outer_loop.revert`: an improving experiment no longer
AUTO-keeps — :func:`~teatree.loops.outer_loop.measure.measure_and_decide` parks it
in ``KEEP_PENDING``, :func:`ask_keep` records the keep
:class:`~teatree.core.models.deferred_question.DeferredQuestion` (so a human visibly
ratifies the retention rather than the loop silently keeping its own change), and
:func:`resolve_keep` is the operator's close-out — it consumes that question and
drives the experiment to the terminal ``KEPT`` state, freeing the max-concurrent
slot for the next experiment.

:func:`ask_keep` consults the taint-floored :func:`approval_policy` seam for the
``outer_loop_keep`` action class. The experiment is factory-internal (owner-taint),
so the floor passes to the dial; the #116 empty dial still returns ``ASK``, keeping
every keep human-gated. #119 injects a permissive owner-taint dial here to
auto-answer (``resolved_via`` policy) — a relaxation that leaves
:meth:`OuterLoopExperiment.record_kept`'s consumed-question guard untouched, because
an auto-answer is still a *consumed answer*, only recorded by policy rather than a
human.
"""

from teatree.core.models import DeferredQuestion, OuterLoopExperiment
from teatree.core.models.approval_dial import auto_answer_by_policy, policy_dial
from teatree.core.models.approval_policy import OUTER_LOOP_KEEP, Decision, Dial, approval_policy
from teatree.core.models.provenance import Provenance

KEEP_ACTION_CLASS = OUTER_LOOP_KEEP


def ask_keep(experiment: OuterLoopExperiment, *, dial: Dial | None = None) -> DeferredQuestion:
    """Record the keep question and bind it (the experiment must be ``KEEP_PENDING``).

    Consults the #119 per-action-class :func:`policy_dial` (unless a *dial* is injected
    for a test) for an owner-taint ``outer_loop_keep``. When it returns ``AUTO_APPROVE``
    the recorded question is auto-answered by policy (``resolved_via='policy'`` + audit) —
    a graduation that still leaves ``record_kept``'s consumed-question guard intact.
    """
    question = DeferredQuestion.record(
        f"Outer-loop experiment #{experiment.pk} improved {experiment.target_provider_id}: "
        f"{experiment.decision_reason}. Approve to keep it via `t3 outer resolve-keep {experiment.pk}`.",
        options_hash=f"outer_loop_keep:{experiment.pk}",
    )
    experiment.attach_keep_question(question)
    if approval_policy(KEEP_ACTION_CLASS, Provenance.OWNER, dial=dial or policy_dial) is Decision.AUTO_APPROVE:
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
