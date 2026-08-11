"""AST audit: a locked read only excludes writers where ``BEGIN IMMEDIATE`` covers it.

``select_for_update()`` is a silent no-op on the production engine —
``teatree.db.sqlite3_boundary`` reports ``has_select_for_update = False``, so
Django drops the ``FOR UPDATE`` clause without error. Every read-modify-write in
this codebase that names a row lock is in fact excluded by
``transaction_mode: "IMMEDIATE"`` (``settings.SQLITE_WRITE_SERIALIZATION_OPTIONS``),
which makes each ``transaction.atomic()`` take SQLite's reserved write lock from
BEGIN through COMMIT.

That substitution holds only where an ``atomic()`` encloses the read, and only
for the plain call. This module walks ``src/teatree`` and reports the two shapes
where it does not:

*   a locked read **outside** ``transaction.atomic()`` — no BEGIN IMMEDIATE is
    ever taken, so nothing excludes a concurrent writer. Django's own
    "select_for_update cannot be used outside of a transaction" guard is itself
    gated on ``has_select_for_update``, so on this backend the mistake raises
    nothing at all.
*   a locked read passing ``skip_locked`` / ``nowait`` — semantics
    ``IMMEDIATE`` cannot reproduce (it *blocks* where they promise to return),
    and which the backend drops silently. ``of=`` is not in that class:
    ``IMMEDIATE`` takes a strictly wider lock than any narrowing it asks for.

Never-lockout: a helper whose CALLER owns the transaction declares that contract
with a same-line :data:`CALLER_ATOMIC_PRAGMA` comment. The exemption is per call
site and visible in the diff that introduces it; the conformance lane pins the
set of exempted helpers so it cannot grow silently.
"""

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: Same-line marker declaring that the enclosing helper's caller holds the
#: ``transaction.atomic()`` for the whole read-modify-write.
CALLER_ATOMIC_PRAGMA = "select-for-update: caller-atomic"

#: Keyword arguments whose semantics ``BEGIN IMMEDIATE`` cannot supply.
UNEMULATABLE_OPTIONS: frozenset[str] = frozenset({"skip_locked", "nowait"})

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class Coverage(StrEnum):
    """How a locked read obtains the exclusion it names."""

    ATOMIC_BLOCK = "atomic_block"
    ATOMIC_DECORATOR = "atomic_decorator"
    CALLER_CONTRACT = "caller_contract"
    NONE = "none"


@dataclass(frozen=True)
class LockedRead:
    """One ``select_for_update()`` call site and the exclusion behind it."""

    path: Path
    lineno: int
    enclosing: str
    coverage: Coverage
    options: tuple[str, ...]


def _is_atomic(node: ast.expr) -> bool:
    """True for ``transaction.atomic``/``atomic``, called or bare."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr == "atomic"
    return isinstance(target, ast.Name) and target.id == "atomic"


def _is_locked_read(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "select_for_update"


def _parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> Iterator[ast.AST]:
    current = node
    while current in parents:
        current = parents[current]
        yield current


def _enclosing_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    for ancestor in _ancestors(node, parents):
        if isinstance(ancestor, _FUNCTION_NODES):
            return getattr(ancestor, "name", "<lambda>")
    return "<module>"


def _coverage(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> Coverage:
    """Resolve the enclosing ``atomic()``, stopping at the function boundary.

    A nested function or lambda does NOT inherit an outer ``with atomic()``: it
    may be called long after that block committed, so the search stops at its
    own ``def`` and considers only that function's decorators.
    """
    for ancestor in _ancestors(node, parents):
        if isinstance(ancestor, ast.With | ast.AsyncWith) and any(
            _is_atomic(item.context_expr) for item in ancestor.items
        ):
            return Coverage.ATOMIC_BLOCK
        if isinstance(ancestor, _FUNCTION_NODES):
            decorators = getattr(ancestor, "decorator_list", [])
            return Coverage.ATOMIC_DECORATOR if any(_is_atomic(d) for d in decorators) else Coverage.NONE
    return Coverage.NONE


def _declares_caller_contract(lines: list[str], node: ast.Call) -> bool:
    span = lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
    return any(CALLER_ATOMIC_PRAGMA in line for line in span)


def audit_source(source: str, path: Path) -> list[LockedRead]:
    """Census every locked read in *source*, in file order."""
    tree = ast.parse(source, filename=str(path))
    parents = _parent_map(tree)
    lines = source.splitlines()
    sites: list[LockedRead] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_locked_read(node):
            continue
        coverage = _coverage(node, parents)
        if coverage is Coverage.NONE and _declares_caller_contract(lines, node):
            coverage = Coverage.CALLER_CONTRACT
        sites.append(
            LockedRead(
                path=path,
                lineno=node.lineno,
                enclosing=_enclosing_name(node, parents),
                coverage=coverage,
                options=tuple(sorted(k.arg for k in node.keywords if k.arg in UNEMULATABLE_OPTIONS)),
            )
        )
    return sorted(sites, key=lambda site: site.lineno)


def audit_file(path: Path) -> list[LockedRead]:
    return audit_source(path.read_text(encoding="utf-8"), path)


def audit_tree(roots: Iterable[Path]) -> list[LockedRead]:
    """Census every locked read under *roots*."""
    sites: list[LockedRead] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            sites.extend(audit_file(path))
    return sites


def violation_reason(site: LockedRead) -> str:
    """Why ``IMMEDIATE`` does not cover *site*; empty when it does."""
    if site.options:
        return (
            f"passes {', '.join(site.options)} — IMMEDIATE blocks where it promises to return, "
            "and the kwarg is silently dropped"
        )
    if site.coverage is Coverage.NONE:
        return "outside transaction.atomic() — BEGIN IMMEDIATE is never taken, so nothing excludes a concurrent writer"
    return ""


def violations(sites: Iterable[LockedRead]) -> list[LockedRead]:
    return [site for site in sites if violation_reason(site)]


def render(site: LockedRead) -> str:
    reason = violation_reason(site) or f"covered by transaction.atomic() ({site.coverage.value})"
    return f"{site.path}:{site.lineno} in {site.enclosing}() — {reason}"
