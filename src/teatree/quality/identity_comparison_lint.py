"""AST guard: a normalized identity must never be compared raw at the other end.

The identity-normalization ('slug bug') family from the teatree integration
audit: a logical identity that has a canonical form — a phase alias, an overlay
name, a repo slug, a singleton process name — is normalized at one end of a
comparison and left raw at the other, so two spellings of the SAME identity
compare unequal and the branch silently mis-fires. ``normalize_phase("plan") ==
"planning"`` is ``True``; the raw ``"plan" == "planning"`` is ``False`` — and
that ``False`` silently skipped the ``PlanArtifact`` record, wedging the ticket
at ``STARTED`` with coding edits denied (audit #20).

This module walks the ``src/teatree`` AST and reports a raw equality/identity
comparison (``==`` / ``!=`` / ``is`` / ``is not``) against a registered family's
canonical member, where the OTHER operand is not routed through that family's
normalizer.

Extension points — the registry is additive by construction
=============================================================

Every family is declared once in :data:`IDENTITY_COMPARISON_FAMILIES` and the
checker is family-agnostic, so a new identity class plugs in by APPENDING one
:class:`IdentityFamily` — no checker edit. Two families ship today (PHASE, the
:func:`~teatree.core.modelkit.phases.normalize_phase` guard, and OVERLAY, the
``.overlay``-vs-bare-literal guard). The audit names two more that plug in
against this same registry later:

*   MW-B's SLUG family — canonical members are repo-slug forms, normalizers are
    ``normalize_repo_slug`` / ``resolve_pr_repo_slug``. Register as a
    ``LITERAL_MEMBER`` family (or an ``IDENTITY_ATTR`` guard on ``.slug``).
*   A singleton-literal guard — the ``"worker"`` process-singleton name drift.
    Register as a ``LITERAL_MEMBER`` family with an EMPTY
    ``normalizer_calls`` set: with no normalizer, *any* comparison against the
    member literal is a violation, forcing callers onto the named constant.

Never-lockout: a line carrying the ``# identity-lint: ok`` pragma is exempt (the
narrow, explicit allowlist for an intentional raw comparison).
"""

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

EXEMPT_PRAGMA = "identity-lint: ok"

#: The equality/identity operators a raw identity comparison uses — the exact
#: ``==`` / ``!=`` / ``is`` / ``is not`` bug shape. ``in`` / ``not in`` are out
#: of scope (no real target uses them un-normalized) so a membership test
#: against a frozenset of spellings is never mistaken for the bug.
_EQUALITY_OPS: tuple[type[ast.cmpop], ...] = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)


class FamilyKind(StrEnum):
    """How a family recognises the identity operand of a comparison.

    ``LITERAL_MEMBER`` — one operand is a canonical member string
    (``"planning"``); the OTHER operand is the raw side that must be normalized.
    ``IDENTITY_ATTR`` — one operand is an identity-bearing attribute
    (``x.overlay``) compared against a bare non-exempt string literal; the
    attribute is the raw side (it can only be clean when wrapped in a
    normalizer call, which is a ``Call`` node, not an ``Attribute``).
    """

    LITERAL_MEMBER = "literal_member"
    IDENTITY_ATTR = "identity_attr"


@dataclass(frozen=True)
class IdentityFamily:
    """One identity class whose canonical form must be used at every comparison.

    ``normalizer_calls`` names the functions that canonicalize the raw side; an
    empty set means the member may never be compared at all (use the named
    constant). ``exempt_literals`` are string operands that never require
    normalization (a blank/ambient sentinel like ``""``).
    """

    name: str
    kind: FamilyKind
    normalizer_calls: frozenset[str]
    literal_members: frozenset[str] = frozenset()
    identity_attrs: frozenset[str] = frozenset()
    exempt_literals: frozenset[str] = field(default_factory=lambda: frozenset({""}))


#: Every distinctive canonical phase token — the ``CANONICAL_PHASES`` gerunds
#: plus every dispatchable reactive phase (``SUBAGENT_BY_PHASE`` keys). VENDORED
#: as a literal because ``teatree.quality`` is a foundation layer that must not
#: import the ``teatree.core.modelkit`` domain (tach). The drift-detecting parity
#: test ``TestPhaseMembersMatchTheSsot`` fails if this set diverges from the live
#: phase vocabulary, so the vendored copy cannot silently go stale.
#:
#: The short verbs (``code``/``test``/``review``/``ship``) are deliberately
#: excluded — they are common non-phase strings (a lifecycle-skill name, a
#: do-step name), so a comparison against them is not necessarily a phase one.
PHASE_MEMBERS: frozenset[str] = frozenset(
    {
        "planning",
        "scoping",
        "coding",
        "testing",
        "reviewing",
        "shipping",
        "retro",
        "requesting_review",
        "e2e",
        "e2e_reviewing",
        "answering",
        "scanning_news",
        "bughunt",
        "debugging",
        "codex_reviewing",
        "codex_adversarial_reviewing",
        "critic_reviewing",
    }
)


PHASE_FAMILY = IdentityFamily(
    name="phase",
    kind=FamilyKind.LITERAL_MEMBER,
    normalizer_calls=frozenset({"normalize_phase"}),
    literal_members=PHASE_MEMBERS,
)

#: The overlay identity is a stored name that must be canonicalized through
#: ``resolve_overlay_name`` / ``_canonical_overlay_name`` before it is matched
#: against a hard-coded overlay-name literal (the recurrence class behind
#: #24's wrong-overlay resolution). A ``.overlay`` compared to another stored
#: name (``ticket.overlay == self.overlay_name``) is a legitimate value match
#: and never flagged — only a comparison against a bare non-empty string is.
OVERLAY_FAMILY = IdentityFamily(
    name="overlay",
    kind=FamilyKind.IDENTITY_ATTR,
    normalizer_calls=frozenset({"resolve_overlay_name", "_canonical_overlay_name"}),
    identity_attrs=frozenset({"overlay"}),
)

#: The additive registry. Append an :class:`IdentityFamily` to guard a new
#: identity class — the checker needs no change.
IDENTITY_COMPARISON_FAMILIES: tuple[IdentityFamily, ...] = (PHASE_FAMILY, OVERLAY_FAMILY)

_Families = Sequence[IdentityFamily]


@dataclass(frozen=True)
class IdentityViolation:
    """One raw identity comparison the guard rejects."""

    path: Path
    lineno: int
    family: str
    snippet: str


def _called_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_normalizer_call(node: ast.expr, normalizers: frozenset[str]) -> bool:
    return isinstance(node, ast.Call) and _called_name(node.func) in normalizers


def _str_const(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_dot_attr(node: ast.expr, attrs: frozenset[str]) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in attrs


def _assigned_normalizer_names(tree: ast.Module, normalizers: frozenset[str]) -> set[str]:
    """Names bound to a normalizer-call result anywhere in the file.

    A comparison of such a name against a member literal is clean — the value
    was canonicalized at assignment (``phase = normalize_phase(self.phase)``).
    File-wide (not per-scope) on purpose: it only ever admits MORE names as
    clean, so it cannot produce a false positive, and no real module both
    normalizes a name and compares a same-named raw value as a bug.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(node, ast.NamedExpr):
            targets, value = [node.target], node.value
        if value is not None and _is_normalizer_call(value, normalizers):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _literal_member_violation(
    family: IdentityFamily, member_side: ast.expr, raw_side: ast.expr, clean: set[str]
) -> bool:
    """True iff *member_side* is a family member and *raw_side* is un-normalized."""
    if _str_const(member_side) not in family.literal_members:
        return False
    # A walrus operand (``(phase := normalize_phase(raw)) == "x"``) is normalized
    # by its own assigned value — unwrap to the bound expression.
    if isinstance(raw_side, ast.NamedExpr):
        raw_side = raw_side.value
    # Comparing two constants (``"coding" == "planning"``) is degenerate, not the
    # identity bug — only a dynamic raw operand mis-fires.
    if isinstance(raw_side, ast.Constant):
        return False
    if _is_normalizer_call(raw_side, family.normalizer_calls):
        return False
    return not (isinstance(raw_side, ast.Name) and raw_side.id in clean)


def _identity_attr_violation(family: IdentityFamily, attr_side: ast.expr, literal_side: ast.expr) -> bool:
    """True iff *attr_side* is a raw identity attribute compared to a bare literal."""
    if not _is_dot_attr(attr_side, family.identity_attrs):
        return False
    literal = _str_const(literal_side)
    return literal is not None and literal not in family.exempt_literals


def _pair_violates(family: IdentityFamily, left: ast.expr, right: ast.expr, clean: set[str]) -> bool:
    if family.kind is FamilyKind.LITERAL_MEMBER:
        return _literal_member_violation(family, left, right, clean) or _literal_member_violation(
            family, right, left, clean
        )
    return _identity_attr_violation(family, left, right) or _identity_attr_violation(family, right, left)


def scan_source(source: str, path: Path, families: _Families = IDENTITY_COMPARISON_FAMILIES) -> list[IdentityViolation]:
    """Report every raw identity comparison in *source* against *families*."""
    tree = ast.parse(source, filename=str(path))
    pragma_lines = {i for i, line in enumerate(source.splitlines(), start=1) if EXEMPT_PRAGMA in line}
    clean_by_family = {f.name: _assigned_normalizer_names(tree, f.normalizer_calls) for f in families}
    findings: list[IdentityViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            if not isinstance(op, _EQUALITY_OPS):
                continue
            left, right = operands[index], operands[index + 1]
            for family in families:
                if node.lineno in pragma_lines:
                    continue
                if _pair_violates(family, left, right, clean_by_family[family.name]):
                    findings.append(
                        IdentityViolation(
                            path=path,
                            lineno=node.lineno,
                            family=family.name,
                            snippet=ast.unparse(node),
                        )
                    )
    return findings


def scan_file(path: Path, families: _Families = IDENTITY_COMPARISON_FAMILIES) -> list[IdentityViolation]:
    return scan_source(path.read_text(encoding="utf-8"), path, families)


def _own_module_path() -> Path:
    return Path(__file__).resolve()


def scan_tree(roots: Iterable[Path], families: _Families = IDENTITY_COMPARISON_FAMILIES) -> list[IdentityViolation]:
    """Scan every ``.py`` under *roots*, skipping this guard's own module.

    The guard module names the member literals in its own registry (as the
    ``literal_members`` frozensets), so it is excluded to avoid inspecting the
    spec that defines the check.
    """
    own = _own_module_path()
    findings: list[IdentityViolation] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == own:
                continue
            findings.extend(scan_file(path, families))
    return findings
