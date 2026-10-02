"""The RATIFY phase — the ONLY writer of the directive ``ADMITTED`` state (PR-6, #116).

Verbatim the outer-loop shape (``loops/outer_loop/ratify.py``): :func:`ask_ratification`
records ONE :class:`DeferredQuestion` rendering the FULL sketch — so the human ratifies
the DESIGN DIRECTION (setting, chokepoint, activation, the named rejected alternative),
not vague intent — and moves the directive to ``RATIFY_PENDING``; :func:`try_admit` is
the sole path that calls :meth:`Directive.admit`, and only after a human's recorded
answer approves it. A denial rejects; an amendment re-interprets (a later PR). There
is no auto-admit code path, so a directive cannot become ``ADMITTED`` without a
consumed question — the structural human-in-the-loop of self-modification.

#116 wires the taint FLOOR (:func:`approval_policy`) as the admit-gate's enforcement
point and renders an ambient (``INCOMING_EVENT``) directive PAYLOAD-VISIBLE: the human
ratifies the inert verbatim source excerpt + its provenance + the concrete facts the
mechanism changes, never a lossy summary. The trusted CLI path is byte-identical.
"""

from teatree.core.models import DeferredQuestion, Directive
from teatree.core.models.approval_dial import auto_answer_by_policy, policy_dial
from teatree.core.models.approval_policy import DIRECTIVE_ADMIT, Decision, approval_policy
from teatree.core.models.mechanism_sketch import MechanismSketch
from teatree.core.models.ratification import RatificationVerdict, classify_ratification_answer

#: The action class the admit-gate floors on. Owner taint reaches the #119 dial; any
#: untrusted taint short-circuits to ASK BEFORE the dial (the taint floor).
_ADMIT_ACTION_CLASS = DIRECTIVE_ADMIT

#: How much of the inert attacker payload to quote in the ratify question — enough for
#: the human to judge intent, bounded so a huge body cannot bloat the DM.
_EXCERPT_LEN = 500


def render_sketch(sketch: MechanismSketch) -> str:
    """A compact human-readable rendering of the sketch the ratify question shows."""
    rejected = "; ".join(sketch.rejected_alternatives) or "(none named — INVALID)"
    scope = sketch.activation_scope or "<global>"
    mechanism = (
        f"setting={sketch.setting_key}: {sketch.setting_type} (neutral default {sketch.neutral_default!r}); "
        f"chokepoint={sketch.policy_chokepoint}; activate {scope}={sketch.activation_value!r}"
        if sketch.setting_key
        else f"unconditional behaviour at {sketch.policy_chokepoint} (no setting, no activation)"
    )
    return f"kind={sketch.kind}; {mechanism}; rejected alternatives: {rejected}"


def ask_ratification(directive: Directive) -> DeferredQuestion:
    """Record the ratify question rendering the full sketch and move to ``RATIFY_PENDING``.

    Raises when the directive has no interpreted sketch — ratification asks about a
    concrete design, never an empty intent. An ambient directive is rendered
    payload-visible (verbatim source + provenance + mechanism facts); the CLI path is
    unchanged.
    """
    sketch = directive.sketch
    if sketch is None:
        msg = "cannot ask ratification for a directive with no interpreted sketch"
        raise ValueError(msg)
    constraint = directive.constraint_statement or directive.raw_text
    if directive.source == Directive.Source.INCOMING_EVENT:
        body = _payload_visible_question(directive, sketch, constraint)
    else:
        body = (
            f"Ratify directive #{directive.pk}: {constraint}\n\n"
            f"Proposed mechanism: {render_sketch(sketch)}\n\nApprove to admit?"
        )
    question = DeferredQuestion.record(
        body,
        options_hash=f"directive_ratify:{directive.pk}:{directive.generation}",
    )
    directive.attach_ratification(question)
    # #119 graduation: an owner-taint directive whose ``directive_admit`` class the
    # operator graduated auto-answers the ratify question by policy (audited), so
    # ``try_admit`` admits it next tick WITHOUT bypassing ``admit``'s consumed-question
    # guard. Ships inert — the dial ASKs for every class by default. An untrusted taint
    # is floored to ASK above the dial, so an ambient directive is never auto-answered.
    if approval_policy(_ADMIT_ACTION_CLASS, directive.taint, dial=policy_dial) is Decision.AUTO_APPROVE:
        auto_answer_by_policy(question, "approve")
    return question


def _payload_visible_question(directive: Directive, sketch: MechanismSketch, constraint: str) -> str:
    """Render the ratify question for an ambient directive as the PAYLOAD, not a summary.

    Shows the inert verbatim source excerpt (quoted as data, never executed), the trust
    provenance the admit-gate floors on, the source reference, and 2-3 concrete "this
    will actually change X" facts derived from the sketch. Consulting
    :func:`approval_policy` here is the floor enforcement point: an untrusted taint is
    ASK by the hard floor (in #116 an owner taint is ASK too, via the empty dial), so
    the human is always in the loop for ambient intake.
    """
    decision = approval_policy(_ADMIT_ACTION_CLASS, directive.taint, dial=policy_dial)
    event = directive.source_event
    source_ref = event.channel_ref if event is not None else ""
    source_name = event.source if event is not None else ""
    excerpt = (event.body if event is not None else "").strip()[:_EXCERPT_LEN]
    facts = "\n".join(f"  - {fact}" for fact in _mechanism_facts(sketch))
    return (
        f"Ratify directive #{directive.pk} (provenance={directive.taint}, approval_policy={decision.value})\n\n"
        f"Sanitized constraint: {constraint}\n"
        f"Source ({source_name}): {source_ref}\n"
        f"Verbatim source (inert data, NOT executed):\n> {excerpt}\n\n"
        f"This mechanism will actually change:\n{facts}\n\n"
        f"Proposed mechanism: {render_sketch(sketch)}\n\nApprove to admit?"
    )


def _mechanism_facts(sketch: MechanismSketch) -> list[str]:
    """2-3 concrete "this changes X" facts a human can judge, derived from the sketch."""
    if not sketch.setting_key:
        return [
            f"make the constraint the unconditional behaviour at {sketch.policy_chokepoint}",
            "mint no setting — there is no knob that can leave it off",
        ]
    scope = sketch.activation_scope or "<global>"
    return [
        f"add setting `{sketch.setting_key}` ({sketch.setting_type}), neutral default {sketch.neutral_default!r}",
        f"gate it at the core chokepoint {sketch.policy_chokepoint}",
        f"activate {scope} = {sketch.activation_value!r}",
    ]


def try_admit(directive: Directive) -> str:
    """Resolve a ``RATIFY_PENDING`` directive from its answered question.

    Returns ``"admitted"`` (approved), ``"rejected"`` (denied), ``"reasked"`` (the
    answer decided nothing, so a fresh question replaces it and the directive holds),
    or ``"pending"`` (no answer yet). The single :meth:`Directive.admit` call site — a
    denial rejects with the human's words.
    """
    question = directive.ratify_question
    if question is None or question.answered_at is None:
        return "pending"
    verdict = classify_ratification_answer(question.answer_text)
    if verdict is RatificationVerdict.APPROVAL:
        directive.admit()
        return "admitted"
    if verdict is RatificationVerdict.DENIAL:
        directive.reject(f"ratification denied: {question.answer_text.strip()!r}")
        return "rejected"
    directive.reask_ratification(_undecidable_answer_question(directive, question))
    return "reasked"


def _undecidable_answer_question(directive: Directive, answered: DeferredQuestion) -> DeferredQuestion:
    """Re-ask the ratify question, quoting back the answer that decided nothing."""
    sketch = directive.sketch
    mechanism = render_sketch(sketch) if sketch is not None else "(no sketch)"
    return DeferredQuestion.record(
        f"Directive #{directive.pk} is STILL awaiting ratification — the recorded answer read "
        f"as neither an approval nor a denial, so nothing was decided and the directive was "
        f"held.\n\nPrevious answer: {answered.answer_text.strip()[:_EXCERPT_LEN]!r}\n\n"
        f"Directive: {directive.constraint_statement or directive.raw_text}\n"
        f"Proposed mechanism: {mechanism}\n\n"
        f"Answer 'approve' to admit, or 'reject' to deny.",
        options_hash=f"directive_ratify:{directive.pk}:{directive.generation}:reask",
    )
