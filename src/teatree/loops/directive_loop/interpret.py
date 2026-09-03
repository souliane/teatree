"""The INTERPRET phase — agent dispatch that turns raw text into a typed sketch (PR-6).

Mirrors the ``CriticDispatch`` idiom (``critic_gate.build_critic_contract`` +
``_enqueue_llm_critic``): :func:`build_interpreter_contract` writes the mechanism-design
doctrine down ONCE — the taste the interpreter must apply — and
:func:`dispatch_interpretation` arms the headless ``directive_interpreting`` task
that returns a ``directive_interpretation`` envelope. The server-side recorder
(``directive_interpret_gate``) validates and writes the sketch; this module never
records — it only frames the question.

The doctrine is the anti-hack procedure (§4.0): duplication-check first, find the
CORE seam every overlay flows through (never an overlay-local patch), ship the
constraint as the UNCONDITIONAL behaviour at that seam, and — the N=2 litmus — name
and reject the overlay-local one-off in writing. A setting is the exception, minted
only when a second overlay demonstrably needs a different value (#4181): the
neutral-default doctrine it replaces queued 31 sketches to ship features that would
never run. Both kinds cite a checked-in exemplar the interpreter shapes its sketch after.
"""

from teatree.core.models import DeferredQuestion, Directive, DirectiveDispatch

#: The canonical exemplar for the DEFAULT kind — a constraint that ships as the
#: unconditional behaviour at one core seam, with no knob that can leave it off.
_DEFAULT_EXEMPLAR = (
    "src/teatree/core/models/plan_adequacy.py::mechanism_conforms, enforced unconditionally at "
    "`plan_currency_gate.check_plan_current` (no setting, no activation — a non-conforming plan "
    "blocks coder dispatch for everyone)"
)

#: The exemplar for the EXCEPTIONAL kind — the PR-2 proof-case mechanism, which earns
#: its setting because a second overlay genuinely wants a different cap.
_SETTING_EXEMPLAR = (
    "src/teatree/core/gates/pr_budget_gate.py (setting `max_open_prs_per_repo_per_ticket`, "
    "neutral default 0, chokepoint `_run_ship_gates` + `_ensure_pr.create_or_defer_pr`, "
    "activation via a per-overlay `ConfigSetting` row naming the overlay that wants a different cap)"
)


def build_interpreter_contract(directive: Directive) -> str:
    """The per-directive interpreter contract — the mechanism-design doctrine + the raw text.

    Embeds the ordered anti-hack decision procedure and the required envelope shape,
    so the headless interpreter produces a sketch that names its generic-shape
    decision and its rejected alternatives, or hands back clarifying questions.
    """
    scope = directive.scope_overlay or "<global>"
    return (
        "You are the teatree directive INTERPRETER (read-only). Turn the plain-language "
        f"directive below (scope: {scope}) into ONE typed MechanismSketch, applying this "
        "mechanism-design doctrine in order:\n"
        "1. DUPLICATION FIRST — search for an existing mechanism that already expresses the "
        "constraint. Found ⇒ kind='activation_only' (configure, don't build).\n"
        "2. CORE SEAM, NOT OVERLAY — locate the src/teatree chokepoint every overlay flows "
        "through; never an overlay-local hook. The recorder REJECTS an overlay/contrib chokepoint.\n"
        "3. DEFAULT BEHAVIOUR FIRST — implement the constraint as the UNCONDITIONAL behaviour at "
        "the core seam. Mint a UserSettings field ONLY when a SECOND overlay demonstrably needs a "
        "DIFFERENT value; name that overlay in activation_scope and its value in activation_value. "
        "Absent a named second consumer, kind='default_behaviour' and setting_key MUST be empty. A "
        "setting whose only job is to turn the directive's own behaviour off is REFUSED at the "
        "recorder, as is a setting_policy_gate whose activation_value equals its neutral_default.\n"
        "4. N=2 LITMUS, RECORDED — rejected_alternatives MUST name the overlay-local one-off and "
        "why it fails ('a second overlay wanting a different value would need code'). A sketch "
        "without it is INVALID at the recorder.\n"
        "5. REFACTOR-OR-DECLARE — if the seam is not clean enough, list the prerequisite "
        "refactors; silence is invalid.\n"
        f"Canonical exemplar for the DEFAULT kind: {_DEFAULT_EXEMPLAR}.\n"
        f"Canonical exemplar for the EXCEPTIONAL setting kind: {_SETTING_EXEMPLAR}.\n\n"
        f"Directive (verbatim):\n{directive.raw_text}\n\n"
        "RETURN in the result envelope (the phase has no shell — the orchestrator records it): "
        '`"directive_interpretation": {"interpreter_identity": "<your-id>", "constraint_statement": '
        '"<normative restatement>", "sketch": {"kind": "default_behaviour"|"setting_policy_gate"|"activation_only", '
        '"setting_key": "<identifier, EMPTY for default_behaviour>", "setting_type": "<type>", '
        '"neutral_default": <inert value>, '
        '"policy_chokepoint": "src/teatree/...::<symbol>", '
        '"activation_scope": "<the SECOND overlay; EMPTY for default_behaviour>", '
        '"activation_value": <value>, "rejected_alternatives": ["<overlay-local one-off + why rejected>"], '
        '"acceptance_tests": ["<pytest node id>"], "refactors": []}}`. '
        'If the directive is AMBIGUOUS, return `"clarifying_questions": ["<question>"]` INSTEAD of a sketch.'
    )


def dispatch_interpretation(directive: Directive) -> "DirectiveDispatch | None":
    """Arm one headless interpret task for *directive*'s current generation (idempotent).

    Returns the new dispatch row, or ``None`` when this generation's interpreter is
    already armed — the dedup that keeps a re-tick from spawning a second interpreter.
    """
    return DirectiveDispatch.enqueue(directive=directive, contract=build_interpreter_contract(directive))


def clarifications_answered(directive: Directive) -> bool:
    """Whether every clarify question for the directive's CURRENT generation is answered.

    The interpret recorder parks a ``CLARIFYING`` directive with one
    :class:`DeferredQuestion` per ambiguity, keyed ``directive_clarify:<pk>:<gen>:<n>``.
    Re-interpretation waits until all of THIS generation's questions are answered;
    an unanswered one keeps the directive parked (never a re-dispatch on partial input).
    """
    questions = DeferredQuestion.objects.filter(
        options_hash__startswith=f"directive_clarify:{directive.pk}:{directive.generation}:"
    )
    if not questions.exists():
        return False
    return not questions.filter(answered_at__isnull=True).exists()


def reinterpret_after_clarification(directive: Directive) -> "DirectiveDispatch | None":
    """Bump the generation and arm a fresh interpreter with the answers appended.

    Called once every clarify question for the current generation is answered — the
    ``CLARIFYING`` → re-``INTERPRETED`` round-trip. Bumps ``generation`` first so the
    dispatch dedups on the NEW generation (one fresh interpreter, never a duplicate).

    A live interpreter is checked BEFORE the bump: answering the clarify question also
    resumes the parked interpreter, and bumping past questions that are keyed on the old
    generation would strand the directive in ``CLARIFYING`` with nothing left to answer.
    """
    if DirectiveDispatch.live_interpreter_exists(directive):
        return None
    directive.bump_generation()
    return dispatch_interpretation(directive)
