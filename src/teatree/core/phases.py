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

# Canonical phase -> the ``Ticket`` FSM transition method that records it.
_CANONICAL_TO_TRANSITION: dict[str, str] = {
    "scoping": "scope",
    "coding": "code",
    "testing": "test",
    "reviewing": "review",
    "shipping": "ship",
    "retro": "retrospect",
    "requesting_review": "request_review",
}


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
