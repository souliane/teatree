r"""Consolidated, class-tagged banned-term registry (banned-terms-registry PR 1).

Teatree carries three separate DB-home term sources today, each read by a
different gate:

* ``banned_terms`` — the flat leak list the commit/posting DIFF gate
    (``banned_terms_cli`` / ``banned_terms_scanner``) and the in-process CORE
    leak gate (``fast_push``) scan.
* ``banned_brands`` — the high-confidence list the full-TREE backstop
    (``banned_terms_tree_scan``) scans.
* ``banned_terms_allowlist`` — the company-identifier carve-out.

This module is the single consolidated home the three collapse into: ONE
``banned_term_registry`` row (or the ``$TEATREE_TERM_REGISTRY`` secret) mapping
each term to a CLASS, plus :func:`terms_for_gate` — the one router that returns
the terms a given gate should scan, by class:

===============  ================================  =========================
class            scanned by gates                  legacy source it migrates
===============  ================================  =========================
``leak``         diff + tree + core                ``banned_brands``
``prose_collider``  diff + core                    ``banned_terms``
``tone``         diff                              (none today)
``allow``        the allowlist carve-out           ``banned_terms_allowlist``
===============  ================================  =========================

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

LEAK: Final = "leak"
PROSE_COLLIDER: Final = "prose_collider"
TONE: Final = "tone"
ALLOW: Final = "allow"

TERM_CLASSES: Final[tuple[str, ...]] = (LEAK, PROSE_COLLIDER, TONE, ALLOW)

# Which term CLASSES each scanning gate consumes. ``leak`` is the widest
# (scanned everywhere); ``tone`` is diff-only; the allowlist is its own class.
GATE_CLASSES: Final[dict[str, tuple[str, ...]]] = {
    "diff": (LEAK, PROSE_COLLIDER, TONE),
    "core": (LEAK, PROSE_COLLIDER),
    "tree": (LEAK,),
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
    normalised: dict[str, tuple[str, ...]] = dict.fromkeys(TERM_CLASSES, ())
    for key, value in raw.items():
        term_class = str(key)
        if term_class not in TERM_CLASSES:
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
    ``tree``, the allowlist for ``allow``). When BOTH the registry and the legacy
    source are unset, the legacy resolver RAISES :class:`BannedTermsUnsetError` —
    the gate REFUSES (fail-closed), never scans as empty.
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
    """Build the class-tagged registry from the current three legacy sources.

    ``banned_brands`` → ``leak`` (the high-confidence list, scanned everywhere);
    ``banned_terms`` → ``prose_collider`` (its current diff+core routing, never
    the full tree); ``banned_terms_allowlist`` → ``allow``. ``tone`` starts empty
    (no current source maps to it). ``banned_terms`` is REQUIRED — an unset list
    propagates :class:`BannedTermsUnsetError` so a migration can never
    silently produce an empty registry; ``banned_brands`` is optional (an unset
    brand list is a legitimate no-brands operator and yields an empty ``leak``).
    """
    from teatree.hooks.banned_terms_cli import resolve_banned_terms  # noqa: PLC0415  dual-read cycle
    from teatree.hooks.banned_terms_tree_scan import load_brand_terms  # noqa: PLC0415  dual-read cycle

    terms = resolve_banned_terms(db_path=db_path)
    try:
        brands = load_brand_terms(db_path=db_path)
    except BannedTermsUnsetError:
        brands = ()
    allow = allowlist_terms(db_path=db_path)
    return {
        LEAK: _dedup(brands),
        PROSE_COLLIDER: _dedup(terms),
        TONE: [],
        ALLOW: _dedup(allow),
    }


@dataclass(frozen=True)
class MigrationVerification:
    """Whether a built registry reproduces every effective term the old config yields.

    ``dropped`` — a term the old config scanned that the registry's effective set
    no longer carries (a lossy migration). ``added`` — a term the registry carries
    that no old source had (a fabricated term). ``allow_mismatch`` — the ``allow``
    class does not equal the old allowlist. ``per_gate_drops`` — per gate, the
    terms that gate scanned before but the new class routing would stop scanning.
    ``ok`` is True only when all four are empty: the migration is provably lossless.
    """

    ok: bool
    dropped: tuple[str, ...]
    added: tuple[str, ...]
    allow_mismatch: bool
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
        for gate, missing in self.per_gate_drops.items():
            lines.append(f"gate {gate!r} would stop scanning: {', '.join(missing)}")
        return "\n".join(lines)


def verify_migration(registry: dict[str, list[str]], *, db_path: Path | None = None) -> MigrationVerification:
    """Verify *registry* reproduces every effective term the old three sources yield.

    Recomputes the old effective sets (``banned_terms`` for the diff/core gates,
    ``banned_brands`` for the tree gate, the allowlist for ``allow``) and checks the
    registry against them: no term dropped from the union, no term fabricated, the
    allowlist round-trips exactly, and no per-gate term the routing would stop
    scanning. The migration is lossless iff :attr:`MigrationVerification.ok`.
    """
    from teatree.hooks.banned_terms_cli import resolve_banned_terms  # noqa: PLC0415  dual-read cycle
    from teatree.hooks.banned_terms_tree_scan import load_brand_terms  # noqa: PLC0415  dual-read cycle

    old_ban = set(resolve_banned_terms(db_path=db_path))
    try:
        old_brands = set(load_brand_terms(db_path=db_path))
    except BannedTermsUnsetError:
        old_brands = set()
    old_allow = set(allowlist_terms(db_path=db_path))

    normalised = _normalise_registry(registry)
    new_ban = set(normalised[LEAK]) | set(normalised[PROSE_COLLIDER]) | set(normalised[TONE])
    old_all = old_ban | old_brands

    dropped = old_all - new_ban
    added = new_ban - old_all
    allow_mismatch = set(normalised[ALLOW]) != old_allow

    per_gate_drops: dict[str, tuple[str, ...]] = {}
    for gate, old_set in (("diff", old_ban), ("core", old_ban), ("tree", old_brands)):
        missing = old_set - set(_classes_union(normalised, gate))
        if missing:
            per_gate_drops[gate] = tuple(sorted(missing))

    ok = not dropped and not added and not allow_mismatch and not per_gate_drops
    return MigrationVerification(
        ok=ok,
        dropped=tuple(sorted(dropped)),
        added=tuple(sorted(added)),
        allow_mismatch=allow_mismatch,
        per_gate_drops=per_gate_drops,
    )
