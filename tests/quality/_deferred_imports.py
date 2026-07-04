"""Shared per-file deferred-import counter + peg ledger (the intra-package ratchets).

A function-scoped ``from teatree.<pkg>... import`` hides an intra-package edge
from tach's acyclic guard (tach cannot see cycles WITHIN a single node). The
per-file peg map ``deferred_import_pegs.toml`` records how many such deferred
imports each source file may carry; a file not listed pegs at 0. Over-peg blocks
(name the file), under-peg banks (lower the entry). Per-file keying makes the
ledger set-union mergeable: two disjoint per-file peg bumps never collide, and
same-file contention surfaces as a git textual conflict instead of a post-merge
red — the property a single ``_FROZEN`` integer could not offer.

Extracted from the two duplicated AST walkers in the intra-core / intra-loop
ratchet tests so the counting logic has one home.
"""

import ast
import dataclasses
import tomllib
from collections.abc import Mapping
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PEGS_TOML = Path(__file__).resolve().parent / "deferred_import_pegs.toml"


def _in_function_scope(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
            return True
        cur = parents.get(cur)
    return False


def count_deferred_imports(source: Path, prefix: str) -> int:
    """Function-scoped imports in *source* whose target module starts with *prefix*."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith(prefix) and _in_function_scope(node, parents):
                count += 1
        elif isinstance(node, ast.Import) and _in_function_scope(node, parents):
            count += sum(1 for alias in node.names if alias.name.startswith(prefix))
    return count


def per_file_counts(pkg_root: Path, prefix: str, *, repo_root: Path = _REPO_ROOT) -> dict[str, int]:
    """Map ``<repo-relative path> -> deferred-import count`` for every file with a nonzero count."""
    counts: dict[str, int] = {}
    for py in sorted(pkg_root.rglob("*.py")):
        n = count_deferred_imports(py, prefix)
        if n:
            counts[py.relative_to(repo_root).as_posix()] = n
    return counts


def load_pegs(table: str, *, toml_path: Path = _PEGS_TOML) -> dict[str, int]:
    """Load the ``[<table>]`` peg map from the ledger TOML (unlisted files peg at 0)."""
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    raw = data.get(table, {})
    return {str(path): int(peg) for path, peg in raw.items()}


@dataclasses.dataclass(frozen=True)
class PegDrift:
    over_peg: tuple[tuple[str, int, int], ...]
    under_peg: tuple[tuple[str, int, int], ...]

    @property
    def ok(self) -> bool:
        return not self.over_peg and not self.under_peg

    def over_lines(self) -> list[str]:
        return [f"  - {path}: {live} deferred import(s) over its peg of {peg}" for path, live, peg in self.over_peg]

    def under_lines(self) -> list[str]:
        return [
            f"  - {path}: {live} deferred import(s), under its peg of {peg} — lower it to {live} to bank"
            for path, live, peg in self.under_peg
        ]


def diff_pegs(live: Mapping[str, int], pegs: Mapping[str, int]) -> PegDrift:
    """Compare live per-file counts against the pegs (a file absent from either side pegs at 0)."""
    over: list[tuple[str, int, int]] = []
    under: list[tuple[str, int, int]] = []
    for path in sorted(set(live) | set(pegs)):
        live_count = live.get(path, 0)
        peg = pegs.get(path, 0)
        if live_count > peg:
            over.append((path, live_count, peg))
        elif live_count < peg:
            under.append((path, live_count, peg))
    return PegDrift(over_peg=tuple(over), under_peg=tuple(under))
