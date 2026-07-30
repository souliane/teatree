r"""Consolidated, class-tagged banned-term registry — the single term-source home.

Teatree carries four separate DB-home term sources, each read by a different gate:

* ``banned_terms`` — the flat leak list the commit/posting DIFF gate
    (``banned_terms_cli`` / ``banned_terms_scanner``) and the in-process CORE
    leak gate (``fast_push``) scan.
* ``banned_brands`` — the high-confidence list the full-TREE backstop
    (``banned_terms_tree_scan``) scans.
* ``banned_terms_allowlist`` — the company-identifier carve-out.
* ``overlay_leak_terms`` — the BLUEPRINT § 1 "core stays generic" gate
    (``check_no_overlay_leak`` / ``fast_push``) that keeps overlay-specific names
    out of the scanned core roots.

This module is the single consolidated home the four collapse into: ONE
``banned_term_registry`` row (or the ``$TEATREE_TERM_REGISTRY`` secret) mapping
each term to a CLASS, plus :func:`terms_for_gate` — the one router that returns
the terms a given gate should scan, by class. The class taxonomy (the canonical
reference table is ``docs/blueprint/configuration.md`` § 10.6):

================  ===============================  =========================  ===============
class             scanned by gate                  legacy source it migrates  unset behaviour
================  ===============================  =========================  ===============
``leak``          diff + tree + core               ``banned_brands``          fail-loud
``prose_collider``  diff + core                    ``banned_terms``           fail-loud
``tone``          diff                             (none today)               inert
``overlay``       overlay (core-generic gate)      ``overlay_leak_terms``     inert
``allow``         the allowlist carve-out          ``banned_terms_allowlist``  inert
================  ===============================  =========================  ===============

The ``leak`` and ``prose_collider`` classes fail LOUD when both the registry and
their legacy source are unset (:class:`BannedTermsUnsetError`); ``tone``,
``overlay`` and ``allow`` are optional and default to an empty, inert set (the
overlay pass is inert-when-empty by design — an operator with no overlay-scoped
names is a legitimate no-op, never a refusal).

**Dual-read, registry-first.** :func:`terms_for_gate` (and the legacy
:func:`banned_terms_cli.resolve_banned_terms` / :func:`banned_terms_tree_scan.load_brand_terms`)
read the NEW registry when it is present, ELSE fall back to the OLD per-source
config. With the registry unset — today's state, the DB row and the secret both
absent — every read falls straight through to the legacy source, so behaviour is
byte-identical to before this module existed. The registry becomes authoritative
only once the operator populates it at cutover (PR 2). When BOTH the registry and
the legacy source are unset, the legacy resolver RAISES
:class:`BannedTermsUnsetError` — the gate REFUSES (fail-closed), never scans as
empty.

The public repo ships with the registry unset; each operator populates it locally
(``t3 banned-terms migrate-registry`` produces the class-tagged value from the
current three sources, self-verifying it drops no term). The DB store is PRIVATE
to the operator, so the customer/brand codenames live in the DB exactly like the
three sources it consolidates; ``SECRET_SETTINGS`` keeps it out of a shared
``config_setting export``.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.config import cold_reader
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError
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
    "MigrationVerification",
    "allowlist_terms",
    "build_registry_from_legacy",
    "class_of_term",
    "export_scan_terms",
    "load_registry",
    "registry_terms_for_gate",
    "terms_for_gate",
    "verify_migration",
]

#: The overlay-leak class. It is registry-only (no :mod:`leak_policy` surface routes
#: to it) because the BLUEPRINT § 1 "core stays generic" gate is a distinct concern
#: from the publish-surface leak gates :func:`leak_policy.decide` governs. Its legacy
#: source is the ``overlay_leak_terms`` row.
OVERLAY: Final = "overlay"

#: The registry's recognised class set: the :mod:`leak_policy` publish-surface taxonomy
#: PLUS :data:`OVERLAY`. ``_normalise_registry`` keeps exactly these top-level keys; any
#: other key is ignored (not a leak source).
REGISTRY_TERM_CLASSES: Final[tuple[str, ...]] = (*TERM_CLASSES, OVERLAY)

# Which term CLASSES each scanning gate consumes, DERIVED from the one policy
# (:func:`teatree.hooks.leak_policy.classes_for_surface`) rather than restated —
# the gate names are the legacy keys the dual-read callers already pass. The
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
    must never silently degrade to an empty ban set, exactly as an unloadable
    legacy list does. Unknown top-level keys are ignored; an absent class defaults
    to empty.
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
    is the "registry unset" signal every dual-read falls back on. A set-but-
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
    raw = cold_reader.read_setting(_REGISTRY_KEY, db_path=db_path)
    if raw is None:
        return None
    return _normalise_registry(raw)


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


def registry_terms_for_gate(gate: str, *, db_path: Path | None = None) -> tuple[str, ...] | None:
    """The registry terms for *gate*, or ``None`` when the registry is unset.

    The registry-only half of the dual-read: a caller inserts this between its env
    override and its legacy DB row, so a present registry wins and an absent one
    (``None``) leaves the caller on its legacy source. Fail-closed on a malformed
    registry (propagates :class:`BannedTermsUnsetError`).
    """
    registry = load_registry(db_path=db_path)
    if registry is None:
        return None
    return _classes_union(registry, gate)


def terms_for_gate(gate: str, *, db_path: Path | None = None) -> tuple[str, ...]:
    """Return the banned terms *gate* should scan — the consolidated dual-read entry.

    Registry-first: when the consolidated registry is present, return the union of
    the classes *gate* consumes (``GATE_CLASSES``, or the ``allow`` carve-out).
    When the registry is unset, fall back to the OLD per-gate source
    (``resolve_banned_terms`` for ``diff``/``core``, ``load_brand_terms`` for
    ``tree``, ``overlay_leak_terms`` for ``overlay``, the allowlist for ``allow``).
    When BOTH the registry and a fail-loud gate's legacy source (``diff``/``core``/
    ``tree``) are unset, the legacy resolver RAISES :class:`BannedTermsUnsetError` —
    the gate REFUSES (fail-closed), never scans as empty. The optional gates
    (``overlay``/``allow``) instead yield an empty, inert set.
    """
    registry = load_registry(db_path=db_path)
    if registry is not None:
        return _classes_union(registry, gate)
    return _legacy_terms_for_gate(gate, db_path=db_path)


def _legacy_terms_for_gate(gate: str, *, db_path: Path | None = None) -> tuple[str, ...]:
    """The pre-registry source for *gate* — raises on a genuinely-unset ban list."""
    from teatree.hooks.banned_terms_cli import resolve_banned_terms  # noqa: PLC0415  dual-read cycle
    from teatree.hooks.banned_terms_tree_scan import load_brand_terms  # noqa: PLC0415  dual-read cycle

    if gate in {"diff", "core"}:
        return resolve_banned_terms(db_path=db_path)
    if gate == "tree":
        return load_brand_terms(db_path=db_path)
    if gate == "overlay":
        return _legacy_overlay_terms(db_path=db_path)
    if gate == ALLOW:
        return _legacy_allowlist(db_path=db_path)
    msg = f"unknown banned-terms gate {gate!r}"
    raise ValueError(msg)


def _legacy_allowlist(db_path: Path | None = None) -> tuple[str, ...]:
    """The DB-home ``banned_terms_allowlist`` carve-out, empty when unset (optional key)."""
    raw = cold_reader.read_setting("banned_terms_allowlist", db_path=db_path)
    if not isinstance(raw, list):
        return ()
    return tuple(str(entry).strip() for entry in raw if str(entry).strip())


def _legacy_overlay_terms(db_path: Path | None = None) -> tuple[str, ...]:
    """The DB-home ``overlay_leak_terms`` row — empty when unset (inert, NEVER raises).

    The overlay pass is inert-when-empty by design (an operator with no overlay-scoped
    names is a legitimate no-op), so an unset row yields an empty tuple exactly like the
    allowlist — never a :class:`BannedTermsUnsetError`.
    """
    raw = cold_reader.read_setting("overlay_leak_terms", db_path=db_path)
    if not isinstance(raw, list):
        return ()
    return tuple(str(entry).strip() for entry in raw if str(entry).strip())


def export_scan_terms(*, db_path: Path | None = None) -> tuple[str, ...]:
    """Every ban-class term for the config-export content scan; fail-safe to empty.

    Registry-first: the order-stable union of the four ban classes (``leak`` +
    ``prose_collider`` + ``tone`` + ``overlay``; the ``allow`` carve-out is not a ban
    source, so it is excluded). When the registry is unset, the legacy ``banned_terms``
    + ``banned_brands`` rows — the two the export scanned before the registry existed.
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
    return _legacy_export_scan_terms(db_path=db_path)


def _legacy_export_scan_terms(db_path: Path | None = None) -> tuple[str, ...]:
    """The pre-registry export scan set: ``banned_terms`` + ``banned_brands``, fail-safe.

    Routes through the two legacy resolvers rather than reading their rows directly, so
    the registry module stays the single home for legacy-key resolution. A genuinely-
    unset source is caught and skipped (an empty scan set) — the export must not crash
    on a store with no terms.
    """
    from teatree.hooks.banned_terms_cli import resolve_banned_terms  # noqa: PLC0415  dual-read cycle
    from teatree.hooks.banned_terms_tree_scan import load_brand_terms  # noqa: PLC0415  dual-read cycle

    seen: dict[str, None] = {}
    for resolver in (resolve_banned_terms, load_brand_terms):
        try:
            resolved = resolver(db_path=db_path)
        except BannedTermsUnsetError:
            continue
        for term in resolved:
            if term.strip():
                seen.setdefault(term, None)
    return tuple(seen)


def class_of_term(term: str, *, db_path: Path | None = None) -> str:
    """The registry class *term* belongs to, for :func:`teatree.hooks.leak_policy.decide`.

    Falls back to :data:`PROSE_COLLIDER` when the registry is unset or does not carry
    *term*: pre-cutover every legacy ``banned_terms`` entry IS a prose-collider, and a
    term the registry cannot classify must land in a BLOCKING class, never in
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
    """Return the allowlist carve-out — the registry ``allow`` class, else the legacy row.

    The dual-read for the company-identifier carve-out that mirrors
    :func:`terms_for_gate` for the ban classes. Unlike the ban classes the
    allowlist is OPTIONAL: an unset registry AND an unset legacy row both yield an
    empty tuple, never a raise.
    """
    registry = load_registry(db_path=db_path)
    if registry is not None:
        return registry[ALLOW]
    return _legacy_allowlist(db_path=db_path)


def _dedup(terms: tuple[str, ...]) -> list[str]:
    """Order-stable de-duplication of *terms* into a JSON-storable list."""
    seen: dict[str, None] = {}
    for term in terms:
        seen.setdefault(term, None)
    return list(seen)


def build_registry_from_legacy(db_path: Path | None = None) -> dict[str, list[str]]:
    """Build the class-tagged registry from the current four legacy sources.

    ``banned_brands`` → ``leak`` (the high-confidence list, scanned everywhere);
    ``banned_terms`` → ``prose_collider`` (its current diff+core routing, never
    the full tree); ``overlay_leak_terms`` → ``overlay``;
    ``banned_terms_allowlist`` → ``allow``. ``tone`` starts empty (no current source
    maps to it). ``banned_terms`` is REQUIRED — an unset list propagates
    :class:`BannedTermsUnsetError` so a migration can never silently produce an empty
    registry; ``banned_brands`` is optional (an unset brand list is a legitimate
    no-brands operator and yields an empty ``leak``), as are ``overlay_leak_terms``
    and the allowlist (both inert-when-empty).
    """
    from teatree.hooks.banned_terms_cli import resolve_banned_terms  # noqa: PLC0415  dual-read cycle
    from teatree.hooks.banned_terms_tree_scan import load_brand_terms  # noqa: PLC0415  dual-read cycle

    terms = resolve_banned_terms(db_path=db_path)
    try:
        brands = load_brand_terms(db_path=db_path)
    except BannedTermsUnsetError:
        brands = ()
    overlay = _legacy_overlay_terms(db_path=db_path)
    allow = allowlist_terms(db_path=db_path)
    return {
        LEAK: _dedup(brands),
        PROSE_COLLIDER: _dedup(terms),
        TONE: [],
        OVERLAY: _dedup(overlay),
        ALLOW: _dedup(allow),
    }


@dataclass(frozen=True)
class MigrationVerification:
    """Whether a built registry reproduces every effective term the old config yields.

    ``dropped`` — a term the old config scanned that the registry's effective set
    no longer carries (a lossy migration). ``added`` — a term the registry carries
    that no old source had (a fabricated term). ``allow_mismatch`` — the ``allow``
    class does not equal the old allowlist. ``overlay_mismatch`` — the ``overlay``
    class does not equal the old ``overlay_leak_terms``. ``per_gate_drops`` — per
    gate, the terms that gate scanned before but the new class routing would stop
    scanning. ``ok`` is True only when all five are empty: the migration is provably
    lossless.
    """

    ok: bool
    dropped: tuple[str, ...]
    added: tuple[str, ...]
    allow_mismatch: bool
    overlay_mismatch: bool
    per_gate_drops: dict[str, tuple[str, ...]]

    def failure_reason(self) -> str:
        """A human-readable, multi-line reason the migration is lossy, or ``""`` when ok."""
        if self.ok:
            return ""
        lines: list[str] = []
        if self.dropped:
            lines.append(f"dropped terms (in old config, absent from the registry): {', '.join(self.dropped)}")
        if self.added:
            lines.append(f"unexpected terms (in the registry, absent from old config): {', '.join(self.added)}")
        if self.allow_mismatch:
            lines.append("the registry allow class does not match the old banned_terms_allowlist")
        if self.overlay_mismatch:
            lines.append("the registry overlay class does not match the old overlay_leak_terms")
        for gate, missing in self.per_gate_drops.items():
            lines.append(f"gate {gate!r} would stop scanning: {', '.join(missing)}")
        return "\n".join(lines)


def verify_migration(registry: dict[str, list[str]], *, db_path: Path | None = None) -> MigrationVerification:
    """Verify *registry* reproduces every effective term the old three sources yield.

    Recomputes the old effective sets (``banned_terms`` for the diff/core gates,
    ``banned_brands`` for the tree gate, ``overlay_leak_terms`` for the overlay gate,
    the allowlist for ``allow``) and checks the registry against them: no term dropped
    from the union, no term fabricated, the allowlist and the overlay class round-trip
    exactly, and no per-gate term the routing would stop scanning. The migration is
    lossless iff :attr:`MigrationVerification.ok`.
    """
    from teatree.hooks.banned_terms_cli import resolve_banned_terms  # noqa: PLC0415  dual-read cycle
    from teatree.hooks.banned_terms_tree_scan import load_brand_terms  # noqa: PLC0415  dual-read cycle

    old_ban = set(resolve_banned_terms(db_path=db_path))
    try:
        old_brands = set(load_brand_terms(db_path=db_path))
    except BannedTermsUnsetError:
        old_brands = set()
    old_overlay = set(_legacy_overlay_terms(db_path=db_path))
    old_allow = set(allowlist_terms(db_path=db_path))

    normalised = _normalise_registry(registry)
    new_ban = set(normalised[LEAK]) | set(normalised[PROSE_COLLIDER]) | set(normalised[TONE])
    old_all = old_ban | old_brands

    dropped = old_all - new_ban
    added = new_ban - old_all
    allow_mismatch = set(normalised[ALLOW]) != old_allow
    overlay_mismatch = set(normalised[OVERLAY]) != old_overlay

    per_gate_drops: dict[str, tuple[str, ...]] = {}
    for gate, old_set in (("diff", old_ban), ("core", old_ban), ("tree", old_brands), ("overlay", old_overlay)):
        missing = old_set - set(_classes_union(normalised, gate))
        if missing:
            per_gate_drops[gate] = tuple(sorted(missing))

    ok = not dropped and not added and not allow_mismatch and not overlay_mismatch and not per_gate_drops
    return MigrationVerification(
        ok=ok,
        dropped=tuple(sorted(dropped)),
        added=tuple(sorted(added)),
        allow_mismatch=allow_mismatch,
        overlay_mismatch=overlay_mismatch,
        per_gate_drops=per_gate_drops,
    )
