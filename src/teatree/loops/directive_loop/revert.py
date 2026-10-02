"""The REVERT flow — instant config rollback + human-ratified code revert (north-star PR-7).

Mirrors :mod:`teatree.loops.outer_loop.revert`. A directive that failed verification
enters ``REVERT_PENDING`` with its overlay config ALREADY rolled back (the reversible
half — done at :func:`teatree.loops.directive_loop.verify.rollback_and_request_revert`).
:func:`ask_revert` records the revert :class:`DeferredQuestion` (so a ``REVERT_PENDING``
directive visibly asks a human rather than dead-ending and holding the loop forever);
:func:`resolve_revert` is the operator's close-out — it consumes that question, ensures
the config stays cleared, and drives the directive to terminal ``REVERTED``.

The human performs the git revert of the merged mechanism change; the loop drives the
ask, the config rollback, and the terminal state — never a git operation.
"""

from teatree.core.models import DeferredQuestion, Directive
from teatree.loops.directive_loop.configure import clear_activation


def ask_revert(directive: Directive) -> DeferredQuestion:
    """Record the revert question and bind it (the directive must be ``REVERT_PENDING``)."""
    question = DeferredQuestion.record(
        f"Directive #{directive.pk} did not verify: {directive.decision_reason}. "
        f"The overlay config is already rolled back — perform the code revert and close it via "
        f"`t3 directive resolve-revert {directive.pk}`.",
        options_hash=f"directive_revert:{directive.pk}",
    )
    directive.attach_revert_question(question)
    return question


def resolve_revert(directive: Directive, *, revert_sha: str = "") -> None:
    """Close a ``REVERT_PENDING`` directive to terminal ``REVERTED``, config rolled back.

    Ensures a revert question exists (asking one if the tick has not yet), consumes it,
    re-asserts the config rollback (idempotent — a safety net if REVERT_PENDING was
    reached off the verify path), then records the terminal state.
    """
    question = directive.revert_question or ask_revert(directive)
    if question.answered_at is None:
        DeferredQuestion.consume(question.pk, answer="reverted")
        directive.revert_question = DeferredQuestion.objects.get(pk=question.pk)
    clear_activation(directive)
    directive.record_reverted(revert_sha=revert_sha)
