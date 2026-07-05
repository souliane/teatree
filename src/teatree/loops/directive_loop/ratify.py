"""The RATIFY phase — the ONLY writer of the directive ``ADMITTED`` state (PR-6).

Verbatim the outer-loop shape (``loops/outer_loop/ratify.py``): :func:`ask_ratification`
records ONE :class:`DeferredQuestion` rendering the FULL sketch — so the human ratifies
the DESIGN DIRECTION (setting, chokepoint, activation, the named rejected alternative),
not vague intent — and moves the directive to ``RATIFY_PENDING``; :func:`try_admit` is
the sole path that calls :meth:`Directive.admit`, and only after a human's recorded
answer approves it. A denial rejects; an amendment re-interprets (a later PR). There
is no auto-admit code path, so a directive cannot become ``ADMITTED`` without a
consumed question — the structural human-in-the-loop of self-modification.
"""

from teatree.core.models import DeferredQuestion, Directive
from teatree.core.models.mechanism_sketch import MechanismSketch

_APPROVE_TOKENS = frozenset({"approve", "approved", "yes", "y", "1", "ratify", "admit", "ok"})


def render_sketch(sketch: MechanismSketch) -> str:
    """A compact human-readable rendering of the sketch the ratify question shows."""
    rejected = "; ".join(sketch.rejected_alternatives) or "(none named — INVALID)"
    scope = sketch.activation_scope or "<global>"
    return (
        f"kind={sketch.kind}; setting={sketch.setting_key}: {sketch.setting_type} "
        f"(neutral default {sketch.neutral_default!r}); chokepoint={sketch.policy_chokepoint}; "
        f"activate {scope}={sketch.activation_value!r}; rejected alternatives: {rejected}"
    )


def ask_ratification(directive: Directive) -> DeferredQuestion:
    """Record the ratify question rendering the full sketch and move to ``RATIFY_PENDING``.

    Raises when the directive has no interpreted sketch — ratification asks about a
    concrete design, never an empty intent.
    """
    sketch = directive.sketch
    if sketch is None:
        msg = "cannot ask ratification for a directive with no interpreted sketch"
        raise ValueError(msg)
    constraint = directive.constraint_statement or directive.raw_text
    question = DeferredQuestion.record(
        f"Ratify directive #{directive.pk}: {constraint}\n\nProposed mechanism: {render_sketch(sketch)}\n\n"
        f"Approve to admit?",
        options_hash=f"directive_ratify:{directive.pk}:{directive.generation}",
    )
    directive.attach_ratification(question)
    return question


def try_admit(directive: Directive) -> str:
    """Resolve a ``RATIFY_PENDING`` directive from its answered question.

    Returns ``"admitted"`` (approved), ``"rejected"`` (denied), or ``"pending"``
    (no answer yet). The single :meth:`Directive.admit` call site — a denial rejects
    with the human's words.
    """
    question = directive.ratify_question
    if question is None or question.answered_at is None:
        return "pending"
    if _is_approval(question.answer_text):
        directive.admit()
        return "admitted"
    directive.reject(f"ratification denied: {question.answer_text.strip()!r}")
    return "rejected"


def _is_approval(answer: str) -> bool:
    return answer.strip().lower() in _APPROVE_TOKENS
