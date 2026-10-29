"""Cold-safe phase names shared by dispatch and config write authority."""

_PHASE_ALIASES: dict[str, tuple[str, ...]] = {
    "planning": ("plan", "planning"),
    "scoping": ("scope", "scoping"),
    "coding": ("code", "coding"),
    "testing": ("test", "testing"),
    "reviewing": ("review", "reviewing"),
    "shipping": ("ship", "shipping"),
    "retro": ("retro", "retrospect", "retrospecting"),
    "requesting_review": ("request_review", "requesting_review", "request-review"),
    "e2e": ("e2e",),
    "e2e_reviewing": ("e2e-review", "e2e_review", "e2e_reviewing"),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases in _PHASE_ALIASES.items() for alias in aliases
}

REVIEW_PHASES: frozenset[str] = frozenset(
    {"reviewing", "e2e_reviewing", "codex_reviewing", "codex_adversarial_reviewing", "critic_reviewing"}
)


def normalize_phase(phase: str) -> str:
    """Collapse short verbs and historical spellings to a case-insensitive phase token."""
    cleaned = phase.strip().lower()
    return _ALIAS_TO_CANONICAL.get(cleaned, cleaned)


def phase_spellings(phase: str) -> tuple[str, ...]:
    """Include stored aliases when querying rows without a per-row normalization pass."""
    canonical = normalize_phase(phase)
    return _PHASE_ALIASES.get(canonical, (canonical,))
