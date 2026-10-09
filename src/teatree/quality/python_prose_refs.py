"""Resolve teatree-shaped symbol references in Python prose against the tree.

:mod:`teatree.quality.skill_symbol_refs` decides whether a reference resolves;
this module decides which lines are prose, so the same resolver reaches the
docstrings and ``#:`` comments its markdown walk could not read at all.

That gap shipped: a PR moved a 203-line function into a new module and left a
``:func:`` citation on the old home. Nothing else covers the surface —
``--doctest-modules`` collects no items from a module carrying no ``>>>``, the
comment-density gate exempts docstrings outright, and a reader following a stale
citation finds nothing and cannot tell renamed from moved from deleted.

Prose is a module, class, or function docstring, plus a ``#:`` attribute comment.
An ordinary ``#`` comment and a non-docstring string literal are left alone: a
prompt template or a message body names symbols it does not cite. Every other
line is masked to blank before the resolver runs, so line numbers, the
``skill-symbol-ref:`` pragma and the finding shape are the markdown walk's.
"""

import ast
import io
import tokenize
from collections.abc import Iterable, Iterator
from pathlib import Path

from teatree.quality.skill_symbol_refs import INDEXED_PACKAGES, SymbolRefFinding, scan_source

_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_ATTRIBUTE_COMMENT = "#:"


def prose_lines(source: str) -> frozenset[int]:
    """Line numbers carrying a docstring or a ``#:`` comment; empty when unparsable."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return frozenset()
    lines = {lineno for node in ast.walk(tree) for lineno in _docstring_span(node)}
    lines.update(_attribute_comment_lines(source))
    return frozenset(lines)


def scan_python_source(source: str, path: Path, repo_root: Path) -> list[SymbolRefFinding]:
    """Resolve every teatree-shaped reference the module's prose makes."""
    keep = prose_lines(source)
    if not keep:
        return []
    masked = "\n".join(line if lineno in keep else "" for lineno, line in enumerate(source.splitlines(), start=1))
    return scan_source(masked, path, repo_root)


def scan_python_file(path: Path, repo_root: Path) -> list[SymbolRefFinding]:
    return scan_python_source(path.read_text(encoding="utf-8"), path, repo_root)


def scan_python_tree(repo_root: Path, packages: Iterable[str] = INDEXED_PACKAGES) -> list[SymbolRefFinding]:
    """Walk the packages the resolver indexes, so the tree it reads is the tree it knows."""
    findings: list[SymbolRefFinding] = []
    for package in packages:
        for source in sorted((repo_root / package).rglob("*.py")):
            findings.extend(scan_python_file(source, repo_root))
    return findings


def _docstring_span(node: ast.AST) -> range:
    body = getattr(node, "body", None)
    if not isinstance(node, _DOCSTRING_OWNERS) or not isinstance(body, list) or not body:
        return range(0)
    first = body[0]
    if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
        return range(0)
    if not isinstance(first.value.value, str):
        return range(0)
    return range(first.lineno, (first.end_lineno or first.lineno) + 1)


def _attribute_comment_lines(source: str) -> Iterator[int]:
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT and token.string.startswith(_ATTRIBUTE_COMMENT):
                yield token.start[0]
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return
