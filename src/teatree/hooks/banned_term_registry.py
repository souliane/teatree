r"""The single classed banned-term registry for all public and core leak gates.

The private ``banned_term_registry`` row, or ``TEATREE_TERM_REGISTRY`` JSON secret,
contains leak, prose_collider, tone, overlay, and allow lists. A critical
gate refuses an empty scan list; the core overlay-leak scanner also requires
overlay terms. The allow class may be empty.
"""

import json
import os
from pathlib import Path
from typing import Final

from teatree.config import cold_reader
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnreadableError, BannedTermsUnsetError
from teatree.hooks.leak_policy import ALLOW, LEAK, PROSE_COLLIDER, TERM_CLASSES, TONE, Surface, classes_for_surface

__all__ = [
    "ALLOW",
    "GATE_CLASSES",
    "LEAK",
    "OVERLAY",
    "PROSE_COLLIDER",
    "REGISTRY_TERM_CLASSES",
    "TERM_CLASSES",
    "TONE",
    "allowlist_terms",
    "class_of_term",
    "export_scan_terms",
    "load_registry",
    "terms_for_gate",
]

#: The overlay-leak class. It is registry-only (no :mod:`leak_policy` surface routes
#: to it) because the BLUEPRINT § 1 "core stays generic" gate is a distinct concern
#: from the publish-surface leak gates :func:`leak_policy.decide` governs.
OVERLAY: Final = "overlay"

#: The registry's recognised class set: the :mod:`leak_policy` publish-surface taxonomy
#: PLUS :data:`OVERLAY`. ``_normalise_registry`` keeps exactly these top-level keys; any
#: other key is ignored (not a leak source).
REGISTRY_TERM_CLASSES: Final[tuple[str, ...]] = (*TERM_CLASSES, OVERLAY)

# Which term CLASSES each scanning gate consumes, DERIVED from the one policy
# (:func:`teatree.hooks.leak_policy.classes_for_surface`) rather than restated.
# The gate names are the public scanner surfaces. The
# ``overlay`` gate is registry-only (its class is not a publish surface), so it is
# added explicitly rather than derived from a surface.
GATE_CLASSES: Final[dict[str, tuple[str, ...]]] = {
    "diff": classes_for_surface(Surface.DIFF),
    "core": classes_for_surface(Surface.CORE),
    "tree": classes_for_surface(Surface.TREE),
    "overlay": (OVERLAY,),
}

_REGISTRY_KEY: Final = "banned_term_registry"
_REGISTRY_ENV: Final = "TEATREE_TERM_REGISTRY"


def _normalise_registry(raw: object) -> dict[str, tuple[str, ...]]:
    """Coerce a stored/parsed registry value into ``{class: (term, ...)}``.

    A set-but-malformed registry (not a table, or a class whose value is not a
    list) RAISES :class:`BannedTermsUnsetError` — fail-closed: a corrupt registry
    must never silently degrade to an empty ban set. Unknown top-level keys are
    ignored; an absent class defaults to empty, so a registry holding only ``leak``
    still enforces its leak terms.
    """
    if not isinstance(raw, dict):
        msg = f"the {_REGISTRY_KEY} registry is set but not a table ({type(raw).__name__}) — refusing to scan as empty"
        raise BannedTermsUnsetError(msg)
    normalised: dict[str, tuple[str, ...]] = dict.fromkeys(REGISTRY_TERM_CLASSES, ())
    for key, value in raw.items():
        term_class = str(key)
        if term_class not in REGISTRY_TERM_CLASSES:
            continue  # unknown top-level key: ignored, not a leak source
        if not isinstance(value, list):
            msg = f"the {_REGISTRY_KEY} registry class {term_class!r} is not a list — refusing to scan as empty"
            raise BannedTermsUnsetError(msg)
        normalised[term_class] = tuple(str(term).strip() for term in value if str(term).strip())
    return normalised


def load_registry(db_path: Path | None = None) -> dict[str, tuple[str, ...]] | None:
    """Return the consolidated registry as ``{class: (term, ...)}``, or ``None`` when unset.

    ``$TEATREE_TERM_REGISTRY`` (a JSON table, the CI-secret path) takes precedence
    so CI feeds the registry without committing any term; otherwise the DB-home
    ``banned_term_registry`` row via the Django-free
    :mod:`teatree.config.cold_reader` (*db_path* overrides the DB path). ``None``
    is the "registry unset" signal. A set-but-
    malformed registry (invalid JSON, a non-table value) RAISES
    :class:`BannedTermsUnsetError` (fail-closed), never a silent empty.
    """
    env = os.environ.get(_REGISTRY_ENV, "")
    if env.strip():
        try:
            parsed = json.loads(env)
        except json.JSONDecodeError as exc:
            msg = f"${_REGISTRY_ENV} is set but not valid JSON — refusing to scan as empty"
            raise BannedTermsUnsetError(msg) from exc
        return _normalise_registry(parsed)
    read = cold_reader.read_setting_confirmed(_REGISTRY_KEY, db_path=db_path)
    if not read.readable:
        raise BannedTermsUnreadableError.for_store(_REGISTRY_KEY, _REGISTRY_ENV)
    if read.value is None:
        return None
    return _normalise_registry(read.value)


def _classes_union(registry: dict[str, tuple[str, ...]], gate: str) -> tuple[str, ...]:
    """The order-stable, de-duplicated union of the terms in the classes *gate* consumes."""
    if gate == ALLOW:
        return registry[ALLOW]
    classes = GATE_CLASSES.get(gate)
    if classes is None:
        msg = f"unknown banned-terms gate {gate!r}"
        raise ValueError(msg)
    seen: dict[str, None] = {}
    for term_class in classes:
        for term in registry[term_class]:
            seen.setdefault(term, None)
    return tuple(seen)


def terms_for_gate(gate: str, *, db_path: Path | None = None) -> tuple[str, ...]:
    """Return the registry terms consumed by *gate*.

    Missing or empty configuration fails loud for publishing gates. The optional
    overlay and allow classes yield an empty set on an unset registry.
    """
    registry = load_registry(db_path=db_path)
    if registry is not None:
        terms = _classes_union(registry, gate)
        if not terms and gate in {"diff", "core", "tree"}:
            msg = f"{_REGISTRY_KEY} has no terms for the {gate} scan — refusing to scan as empty"
            raise BannedTermsUnsetError(msg)
        return terms
    if gate in {"diff", "core", "tree"}:
        raise BannedTermsUnsetError.for_key(_REGISTRY_KEY, _REGISTRY_ENV)
    if gate in {"overlay", ALLOW}:
        return ()
    msg = f"unknown banned-terms gate {gate!r}"
    raise ValueError(msg)


def export_scan_terms(*, db_path: Path | None = None) -> tuple[str, ...]:
    """Every ban-class term for the config-export content scan; fail-safe to empty.

    The order-stable union of the four ban classes excludes the ``allow`` carve-out.
    Unlike the gates this NEVER raises on a genuinely-unset source: the export is a
    backup command, so a store with no terms configured yields an empty scan set. A
    MALFORMED registry still fails loud (propagates :class:`BannedTermsUnsetError`).
    """
    registry = load_registry(db_path=db_path)
    if registry is not None:
        seen: dict[str, None] = {}
        for term_class in (LEAK, PROSE_COLLIDER, TONE, OVERLAY):
            for term in registry[term_class]:
                if term.strip():
                    seen.setdefault(term, None)
        return tuple(seen)
    return ()


def class_of_term(term: str, *, db_path: Path | None = None) -> str:
    """The registry class *term* belongs to, for :func:`teatree.hooks.leak_policy.decide`.

    Falls back to :data:`PROSE_COLLIDER` when the registry is unset or does not carry
    *term*: an unclassified term must land in a BLOCKING class, never in
    :data:`ALLOW`. Class membership is checked in :data:`TERM_CLASSES` order, so the
    widest-scanned class wins a term listed twice.
    """
    registry = load_registry(db_path=db_path)
    if registry is None:
        return PROSE_COLLIDER
    cleaned = term.strip().lower()
    for term_class in TERM_CLASSES:
        if any(entry.strip().lower() == cleaned for entry in registry[term_class]):
            return term_class
    return PROSE_COLLIDER


def allowlist_terms(db_path: Path | None = None) -> tuple[str, ...]:
    """Return the optional registry ``allow`` class."""
    return terms_for_gate(ALLOW, db_path=db_path)
