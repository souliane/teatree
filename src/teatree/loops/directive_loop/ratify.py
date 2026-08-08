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

import re
from enum import Enum

from teatree.core.models import DeferredQuestion, Directive
from teatree.core.models.approval_dial import auto_answer_by_policy, policy_dial
from teatree.core.models.approval_policy import DIRECTIVE_ADMIT, Decision, approval_policy
from teatree.core.models.mechanism_sketch import MechanismSketch


class RatificationVerdict(Enum):
    """What a human's recorded ratify answer decides — three-valued, never two.

    ``UNRECOGNISED`` is the load-bearing member: an answer the classifier cannot read
    as consent OR as refusal decides nothing, and ``REJECTED`` is terminal with no
    recovery transition, so undecidable must never collapse into denial.
    """

    APPROVAL = "approval"
    DENIAL = "denial"
    UNRECOGNISED = "unrecognised"


_APPROVAL_LEMMAS = frozenset(
    {
        "approve",
        "approved",
        "approves",
        "approval",
        "ratify",
        "ratified",
        "ratifies",
        "admit",
        "admitted",
        "accept",
        "accepted",
        "agreed",
        "yes",
        "yep",
        "yeah",
        "y",
        "ok",
        "okay",
        "lgtm",
        "1",
    }
)

#: Denial VERBS state a refusal wherever they lead the answer.
_DENIAL_VERBS = frozenset(
    {
        "reject",
        "rejected",
        "rejects",
        "deny",
        "denied",
        "denies",
        "decline",
        "declined",
        "declines",
        "disapprove",
        "disapproved",
        "refuse",
        "refused",
        "veto",
        "vetoed",
    }
)

#: Bare refusal markers. ``no`` is also an ordinary determiner ("RATIFIED, NO SETTING"),
#: so these count only when they stand alone as the answer's opening clause.
_BARE_DENIALS = frozenset({"no", "n", "nope", "nah", "0"})


#: ``no`` negates only what it directly modifies ("no approval from me"), never a lemma
#: a clause away — "NO SETTING, RATIFIED" is an approval, not a refusal.
_ADJACENT_NEGATORS = frozenset({"no"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_APOSTROPHE_RE = re.compile(r"['\u2019]")
_SENTENCE_END_RE = re.compile(r"[.!?]")
_CLAUSE_END_RE = re.compile(r"[,;:]")

#: Spelled with their apostrophes and stripped the way :func:`_tokens` strips them, so
#: the set matches the tokens the tokeniser actually emits.
_NEGATORS = frozenset(
    _APOSTROPHE_RE.sub("", word)
    for word in ("not", "never", "cannot", "can't", "won't", "don't", "doesn't", "didn't", "refuse", "without")
)

#: A verdict is stated up front, so an approval lemma buried deeper than this in the
#: opening sentence is prose, not consent — it defers rather than admits.
_LEAD_WINDOW = 2

#: How far back a negator flips an approval lemma into a refusal ("I do not approve").
_NEGATION_WINDOW = 3

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


def classify_ratification_answer(answer: str) -> RatificationVerdict:
    """Read a human's prose ratification as consent, refusal, or neither.

    Conservative in BOTH directions: consent must be stated up front and unnegated
    (so "I do not approve this" refuses, and a passing mention of "approval" deeper in
    a sentence decides nothing), while refusal needs an explicit denial verb or a bare
    "no" standing alone as the opening clause (so "RATIFIED, NO SETTING …" reads as the
    approval it is). Anything else is ``UNRECOGNISED`` and re-asked.
    """
    head = _decision_head(answer)
    opening = _tokens(_CLAUSE_END_RE.split(head, maxsplit=1)[0])
    if len(opening) == 1 and opening[0] in _BARE_DENIALS:
        return RatificationVerdict.DENIAL
    tokens = _tokens(head)
    for index, token in enumerate(tokens):
        if token in _APPROVAL_LEMMAS:
            if _is_negated(tokens, index):
                return RatificationVerdict.DENIAL
            return RatificationVerdict.APPROVAL if index <= _LEAD_WINDOW else RatificationVerdict.UNRECOGNISED
        if token in _DENIAL_VERBS:
            if _is_negated(tokens, index) or index > _LEAD_WINDOW:
                return RatificationVerdict.UNRECOGNISED
            return RatificationVerdict.DENIAL
    return RatificationVerdict.UNRECOGNISED


def _decision_head(answer: str) -> str:
    """The opening sentence of the first non-empty line — where a verdict is stated.

    A ratification body goes on to spell out amendments in prose that legitimately
    contains "do NOT mint …" and "no setting"; reading the whole body would let that
    detail overrule the verdict its own first line already gave.
    """
    lines = [line for line in answer.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    end = _SENTENCE_END_RE.search(lines[0])
    return lines[0][: end.start()] if end else lines[0]


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(_APOSTROPHE_RE.sub("", text.lower()))


def _is_negated(tokens: list[str], index: int) -> bool:
    if any(token in _NEGATORS for token in tokens[max(0, index - _NEGATION_WINDOW) : index]):
        return True
    return index > 0 and tokens[index - 1] in _ADJACENT_NEGATORS
