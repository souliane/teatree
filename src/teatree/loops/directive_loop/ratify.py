"""The RATIFY phase — the ONLY writer of the directive ``ADMITTED`` state (PR-6, #116).

Ratification seam inherited from the retired experiment loop: :func:`ask_ratification`
records ONE :class:`DeferredQuestion` as a short card — fixed prose on what the mechanism
does, the directive's words quoted, Approve / Reject as buttons (the sketch itself stays on the
directive) — and moves the directive to ``RATIFY_PENDING``; :func:`try_admit` is
the sole path that calls :meth:`Directive.admit`, and only after the owner's answer,
recorded on an owner channel, approves it. Any other answer — the MCP tool or the
command line — is re-asked of the owner on Slack. A denial rejects; an
amendment re-interprets (a later PR). There is no auto-admit code path, so a directive
cannot become ``ADMITTED`` without a consumed question — the structural
human-in-the-loop of self-modification.

#116 wires the taint FLOOR (:func:`approval_policy`) as the admit-gate's enforcement
point. An ambient (``INCOMING_EVENT``) directive is quoted by its inert verbatim source
whenever that reads cleanly, says where it came from, and recommends rejecting.
"""

from dataclasses import replace

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion, Directive
from teatree.core.models.approval_dial import auto_answer_by_policy, policy_dial
from teatree.core.models.approval_policy import DIRECTIVE_ADMIT, Decision, approval_policy
from teatree.core.models.mechanism_sketch import MechanismSketch
from teatree.core.models.provenance import Provenance
from teatree.core.models.ratification import RatificationVerdict, classify_ratification_answer

#: The action class the admit-gate floors on. Owner taint reaches the #119 dial; any
#: untrusted taint short-circuits to ASK BEFORE the dial (the taint floor).
_ADMIT_ACTION_CLASS = DIRECTIVE_ADMIT

#: How much of the inert attacker payload to quote in the ratify question — enough for
#: the human to judge intent, bounded so a huge body cannot bloat the DM.
_EXCERPT_LEN = 500

_QUESTION = "Do you approve this change to how the factory works?"
_APPROVE = CardOption("Approve", "I build it and switch it on.")
_REJECT = CardOption("Reject", "I drop the idea and change nothing.")
_UNDECIDABLE = "Your last answer was not a clear yes or no, so the change is on hold."
_NOT_FROM_THE_OWNER = "The last answer did not come from you, so the change is on hold."


def _why(directive: Directive, sketch: MechanismSketch) -> str:
    clause = (
        "adds one setting and switches it on"
        if sketch.setting_key
        else "becomes fixed behaviour with no setting to turn it off"
    )
    if directive.taint == Provenance.OWNER:
        return f"It {clause}."
    return f"It came from an incoming message, not from you, and it {clause}."


def _ratify_card(directive: Directive, *, why: str) -> QuestionCard:
    """The ratify card; an untrusted directive recommends Reject and quotes its source when that reads cleanly."""
    trusted = directive.taint == Provenance.OWNER
    options = (
        (replace(_APPROVE, recommended=True), _REJECT) if trusted else (replace(_REJECT, recommended=True), _APPROVE)
    )
    card = QuestionCard(
        decision=OwnerDecision.ARCHITECTURE,
        checked=(
            "The directive was turned into one concrete mechanism.",
            f"It comes from {'you' if trusted else 'an incoming message'}.",
        ),
        blocker="Only the owner can approve a change to how the factory works.",
        why=why,
        options=options,
    )
    event = directive.source_event
    if (
        not trusted
        and event is not None
        and (quoted := card.with_quote(_QUESTION, event.body.strip()[:_EXCERPT_LEN])).quoted
    ):
        return quoted
    return card.with_quote(_QUESTION, directive.constraint_statement or directive.raw_text)


def ask_ratification(directive: Directive) -> DeferredQuestion:
    """Record the ratify question and move to ``RATIFY_PENDING``.

    Raises when the directive has no interpreted sketch — ratification asks about a
    concrete design, never an empty intent.
    """
    sketch = directive.sketch
    if sketch is None:
        msg = "cannot ask ratification for a directive with no interpreted sketch"
        raise ValueError(msg)
    question = DeferredQuestion.record(
        _QUESTION,
        options_hash=f"directive_ratify:{directive.pk}:{directive.generation}",
        card=_ratify_card(directive, why=_why(directive, sketch)),
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


def try_admit(directive: Directive) -> str:
    """Resolve a ``RATIFY_PENDING`` directive from its answered question.

    Returns ``"admitted"`` (approved), ``"rejected"`` (denied), ``"reasked"`` (the
    answer decided nothing — undecidable, or not given on an owner channel — so a
    fresh question replaces it and the directive holds), or ``"pending"`` (no answer
    yet). The single :meth:`Directive.admit` call site — a denial rejects with the
    human's words.
    """
    question = directive.ratify_question
    if question is None or question.answered_at is None:
        return "pending"
    if not question.answered_on_owner_channel:
        directive.reask_ratification(_reask_question(directive, why=_NOT_FROM_THE_OWNER))
        return "reasked"
    verdict = classify_ratification_answer(question.answer_text)
    if verdict is RatificationVerdict.APPROVAL:
        directive.admit()
        return "admitted"
    if verdict is RatificationVerdict.DENIAL:
        directive.reject(f"ratification denied: {question.answer_text.strip()!r}")
        return "rejected"
    directive.reask_ratification(_reask_question(directive, why=_UNDECIDABLE))
    return "reasked"


def _reask_question(directive: Directive, *, why: str) -> DeferredQuestion:
    """Ask the ratify question again, saying why the last answer decided nothing."""
    return DeferredQuestion.record(
        _QUESTION,
        options_hash=f"directive_ratify:{directive.pk}:{directive.generation}:reask",
        card=_ratify_card(directive, why=why),
    )
