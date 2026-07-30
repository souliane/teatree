"""The ONE leak-gate policy function (#3532).

The banned-terms / leak subsystem used to have no policy function. Eleven
enforcement points each composed their own tuple of (term source x matcher x
visibility resolver x carve-out set x fail direction), so the same decision was
re-derived eleven times and drifted eleven ways. This module is the single home
for the decision the whole subsystem exists to make::

    decide(term_class, visibility, surface) -> BLOCK | WARN | ALLOW

It is TOTAL over (class x visibility x surface) and carries no term data, no
config reads and no I/O — the term registry (:mod:`teatree.hooks.banned_term_registry`),
the matcher (:mod:`teatree.hooks.term_match`) and the visibility resolver
(:mod:`teatree.hooks._repo_visibility`) stay where they are. Being a pure leaf
keeps it importable from the cold PreToolUse hook and from Django-side gates alike.

**Polarity.** ``UNKNOWN`` visibility is folded onto ``PUBLIC``: a destination the
gate cannot PROVE non-public is scanned and blocked. Detection failure never
weakens the gate.

**Surfaces.** ``LOCAL_COMMIT`` is the one surface that downgrades a blocking class
to ``WARN``: a commit reaches no remote until ``git push``, and the #703 pre-push
gate re-scans commit messages before they do. Every other surface is an egress
with no backstop behind it, so it BLOCKS.

**Class routing is derived here, not duplicated.**
:data:`banned_term_registry.GATE_CLASSES` is built from :func:`classes_for_surface`
so the "which classes does this gate scan" question also has exactly one answer.
"""

from enum import Enum
from typing import Final

#: The term taxonomy. It lives HERE rather than in the registry because the
#: policy is what gives a class meaning; :mod:`teatree.hooks.banned_term_registry`
#: re-exports these names and supplies the per-class term DATA.
LEAK: Final = "leak"
PROSE_COLLIDER: Final = "prose_collider"
TONE: Final = "tone"
ALLOW: Final = "allow"

TERM_CLASSES: Final[tuple[str, ...]] = (LEAK, PROSE_COLLIDER, TONE, ALLOW)


class Verdict(Enum):
    """What a leak gate does with a matched term."""

    BLOCK = "block"
    WARN = "warn"
    ALLOW = "allow"


class Visibility(Enum):
    """Three-valued affirmative-public visibility of a resolved destination.

    ``UNKNOWN`` is the fail-CLOSED case a two-valued "public?" boolean collapses
    into "not public -> skip", which let a probe error on a resolvable slug ride
    out unscanned (#3442). It is kept distinct here and folded onto ``PUBLIC``
    by the policy, never onto ``NON_PUBLIC``.
    """

    PUBLIC = "public"
    NON_PUBLIC = "non-public"
    UNKNOWN = "unknown"


class Surface(Enum):
    """Where the scanned text is about to go.

    ``DIFF`` — the PreToolUse posting gate over a publish command's body.
    ``LOCAL_COMMIT`` — a ``git commit`` whose chained segments cannot publish.
    ``CORE`` — the in-process ``fast_push`` leak gate over a commit range.
    ``TREE`` — the whole-tree backstop.
    ``PUBLISH`` — an outbound forge/chat write through the egress chokepoint.
    """

    DIFF = "diff"
    LOCAL_COMMIT = "local_commit"
    CORE = "core"
    TREE = "tree"
    PUBLISH = "publish"


# Which term CLASSES each surface scans. ``leak`` is the widest (scanned
# everywhere); ``tone`` reaches only the two body-level publish surfaces; the
# ``allow`` carve-out class is never scanned FOR, so it appears nowhere.
_SURFACE_CLASSES: Final[dict[Surface, tuple[str, ...]]] = {
    Surface.DIFF: (LEAK, PROSE_COLLIDER, TONE),
    Surface.LOCAL_COMMIT: (LEAK, PROSE_COLLIDER, TONE),
    Surface.CORE: (LEAK, PROSE_COLLIDER),
    Surface.TREE: (LEAK,),
    Surface.PUBLISH: (LEAK, PROSE_COLLIDER, TONE),
}


def classes_for_surface(surface: Surface) -> tuple[str, ...]:
    """The term classes *surface* scans, in registry order."""
    return _SURFACE_CLASSES[surface]


def decide(term_class: str, visibility: Visibility, surface: Surface) -> Verdict:
    """The leak verdict for a term of *term_class* reaching *visibility* via *surface*.

    ``ALLOW`` when the term's class is the carve-out class, when *surface* does not
    scan that class, or when the destination is PROVABLY non-public. Otherwise
    ``WARN`` on ``LOCAL_COMMIT`` (the #703 pre-push gate is the real block) and
    ``BLOCK`` on every egress surface. An unrecognised class RAISES rather than
    resolving ALLOW — an unclassifiable term must never silently pass.
    """
    if term_class not in TERM_CLASSES:
        msg = f"unknown term class {term_class!r}"
        raise ValueError(msg)
    if term_class == ALLOW:
        return Verdict.ALLOW
    if term_class not in classes_for_surface(surface):
        return Verdict.ALLOW
    if visibility is Visibility.NON_PUBLIC:
        return Verdict.ALLOW
    return Verdict.WARN if surface is Surface.LOCAL_COMMIT else Verdict.BLOCK


def scans_on_visibility(visibility: Visibility) -> bool:
    """Whether a gate must run its scan at all for *visibility*.

    The composed predicate the visibility-scoped gates call: everything the gate
    cannot PROVE non-public is scanned, so ``PUBLIC`` and ``UNKNOWN`` both scan
    and only ``NON_PUBLIC`` is skip-eligible.
    """
    return visibility is not Visibility.NON_PUBLIC
