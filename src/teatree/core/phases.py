"""Canonical phase vocabulary — one name set across skills, sessions, FSM (#694).

Skills emit short verbs (``scope``, ``code``, ``test``, ``review``, ``ship``,
``retro``); older code and ``Session._REQUIRED_PHASES`` use gerunds
(``testing``, ``reviewing``) and the short ``retro``. Before #694 these two
vocabularies were never reconciled, so ``visit-phase <id> review`` recorded an
unrecognised phase and never advanced the FSM.

``normalize_phase`` collapses every spelling to one canonical token (the form
already stored in ``Session.visited_phases`` / ``_REQUIRED_PHASES`` so existing
rows keep working). ``phase_transition`` maps a phase to the ``Ticket`` FSM
transition method that records it.
"""

# Canonical token -> every accepted alias (including the canonical itself).
_PHASE_ALIASES: dict[str, tuple[str, ...]] = {
    "planning": ("plan", "planning"),
    "scoping": ("scope", "scoping"),
    "coding": ("code", "coding"),
    "testing": ("test", "testing"),
    "reviewing": ("review", "reviewing"),
    "shipping": ("ship", "shipping"),
    "retro": ("retro", "retrospect", "retrospecting"),
    "requesting_review": ("request_review", "requesting_review", "request-review"),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases in _PHASE_ALIASES.items() for alias in aliases
}

#: The canonical phase vocabulary — the single source of truth a second
#: hand-maintained set (``Session._REQUIRED_PHASES``) must be validated
#: against so the two cannot drift silently (#782).
CANONICAL_PHASES: frozenset[str] = frozenset(_PHASE_ALIASES)

# Canonical phase -> the ``Ticket`` FSM transition method that records it.
_CANONICAL_TO_TRANSITION: dict[str, str] = {
    "planning": "plan",
    "scoping": "scope",
    "coding": "code",
    "testing": "test",
    "reviewing": "review",
    "shipping": "ship",
    "retro": "retrospect",
    "requesting_review": "request_review",
}


#: The single source of truth mapping a ticket ``(role, phase)`` pair to the
#: sub-agent the loop dispatches for it. Every author phase routes to its OWN
#: phase agent — the loop is the per-phase dispatcher, never a single
#: orchestrator that chains coder→tester→reviewer→shipper inline (BLUEPRINT
#: §5.2 / §17.8 invariant 10: phase agents are invoked by the headless
#: executor when a phase task is claimed; the orchestrator does synthesis and
#: dispatch, not execution). Keyed on the canonical phase token so a task
#: stored with any accepted spelling resolves through ``normalize_phase``.
SUBAGENT_BY_PHASE: dict[tuple[str, str], str] = {
    ("author", "planning"): "t3:planner",
    ("reviewer", "reviewing"): "t3:reviewer",
    ("author", "coding"): "t3:coder",
    ("author", "testing"): "t3:tester",
    ("author", "reviewing"): "t3:reviewer",
    ("author", "shipping"): "t3:shipper",
    ("author", "answering"): "t3:answerer",
    ("author", "scanning_news"): "t3:scanning-news",
}

#: The chaining orchestrator must never be the target of an author phase
#: dispatch — that is the shadowing the per-phase restoration removes. A
#: conformance test asserts no author phase routes here.
CHAINING_ORCHESTRATOR: str = "t3:orchestrator"


def subagent_for_phase(role: str, phase: str) -> str:
    """Return the sub-agent for a ticket ``(role, phase)`` pair, or ``""``.

    ``phase`` is normalized so a task stored with a short-verb spelling
    (``code``/``test``/``review``/``ship``) resolves the same as the
    canonical gerund. An empty string means the pair has no registered
    sub-agent (operator triage) — the single authority both the loop
    dispatcher and the ``loop_dispatch`` command consult.
    """
    return SUBAGENT_BY_PHASE.get((role, normalize_phase(phase)), "")


def normalize_phase(phase: str) -> str:
    """Return the canonical token for ``phase``.

    Accepts the short verbs the skills emit and the gerunds older code uses;
    case- and whitespace-insensitive. Unknown phases pass through lowered and
    stripped so visiting a free-form phase still records *something* rather
    than raising.
    """
    cleaned = phase.strip().lower()
    return _ALIAS_TO_CANONICAL.get(cleaned, cleaned)


def phase_spellings(phase: str) -> tuple[str, ...]:
    """Every stored spelling that normalizes to ``phase``'s canonical form.

    Lets a DB query match a phase regardless of which accepted spelling a
    row was stored with (``phase__in=phase_spellings(...)``) without a
    per-row ``normalize_phase`` call. For an unknown phase, returns just
    its own normalized form so callers still get an exact match.
    """
    canonical = normalize_phase(phase)
    return _PHASE_ALIASES.get(canonical, (canonical,))


def phase_transition(phase: str) -> str | None:
    """Return the FSM transition method name for ``phase``, or ``None``.

    ``None`` means the phase has no associated FSM transition (e.g. a
    free-form phase), so callers must not attempt to advance the FSM.
    """
    return _CANONICAL_TO_TRANSITION.get(normalize_phase(phase))
