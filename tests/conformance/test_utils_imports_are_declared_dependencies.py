"""An import-time import under ``teatree/utils/`` must resolve to a DECLARED dependency.

``teatree.utils.dep_skew`` imported ``packaging.requirements`` at module level while
``packaging`` was declared in no ``pyproject.toml``. On a host tool env something else
drags it in transitively, so every local lane stayed green; the container has no such
accident, and since ``doctor/skew_repair`` reaches ``dep_skew``, the missing module took
out the WHOLE ``t3 doctor check`` run — the health surface reporting nothing while
looking merely noisy.

The transitive resolution is what makes this invisible: asserting ``import packaging``
works proves nothing on the very env where the bug cannot reproduce. So the walk compares
the tree's own import-time imports against the DECLARATION, which reads identically in
both venues.

``utils`` is the leaf layer the rest of the tree imports, and the neighbouring
``dep_drift`` holds a deliberate zero-non-stdlib contract, so an undeclared import here
is both the easiest to add unnoticed and the widest in blast radius.
"""

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass

from tests.conformance._declared_dependencies import declared_dependencies, distributions_by_top_level
from tests.conformance._src_tree import REPO_ROOT, SRC_DIR, parsed_modules

UTILS_DIR = SRC_DIR / "utils"

FIRST_PARTY = "teatree"


@dataclass(frozen=True, slots=True)
class UndeclaredImport:
    module: str
    source: str
    resolved: tuple[str, ...]

    @property
    def summary(self) -> str:
        resolved = ", ".join(self.resolved) or "no installed distribution"
        return f"{self.source} imports {self.module!r} at import time, which resolves to {resolved}"


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_children(node: ast.AST) -> Iterator[ast.AST]:
    """No function body runs on import, nor a ``TYPE_CHECKING`` body — but its ``else`` branch always does."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            yield from child.orelse
            continue
        yield child


def _import_time_nodes(node: ast.AST) -> Iterator[ast.AST]:
    for child in _runtime_children(node):
        yield child
        yield from _import_time_nodes(child)


def _import_time_top_levels(tree: ast.Module) -> set[str]:
    top_levels: set[str] = set()
    for node in _import_time_nodes(tree):
        if isinstance(node, ast.Import):
            top_levels |= {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_levels.add(node.module.partition(".")[0])
    return top_levels


def _third_party_imports() -> dict[str, list[str]]:
    """Non-stdlib, non-first-party top-level modules ``utils`` imports, mapped to their sources."""
    imports: dict[str, list[str]] = {}
    for path, tree in parsed_modules(UTILS_DIR):
        for top_level in _import_time_top_levels(tree):
            if top_level == FIRST_PARTY or top_level in sys.stdlib_module_names:
                continue
            imports.setdefault(top_level, []).append(path.relative_to(REPO_ROOT).as_posix())
    return imports


def _undeclared_imports() -> list[UndeclaredImport]:
    declared = declared_dependencies().keys()
    by_top_level = distributions_by_top_level()
    return [
        UndeclaredImport(module, source, tuple(sorted(by_top_level.get(module, frozenset()))))
        for module, sources in sorted(_third_party_imports().items())
        if not by_top_level.get(module, frozenset()) & declared
        for source in sorted(sources)
    ]


def test_import_time_imports_under_utils_resolve_to_a_declared_dependency():
    undeclared = _undeclared_imports()
    assert not undeclared, (
        "these imports run at import time but the distribution behind them is declared in no "
        "[project.dependencies], so they resolve only by transitive accident and are absent from "
        "a clean install: " + "; ".join(entry.summary for entry in undeclared)
    )


def test_the_walk_sees_the_import_that_motivated_the_rule():
    """The control: a walk that saw nothing would pass the assertion above."""
    assert "src/teatree/utils/dep_skew.py" in _third_party_imports().get("packaging", [])


def test_the_walk_sees_an_import_only_the_type_checking_else_branch_runs():
    """The control: skipping the whole ``ast.If`` hides the runtime fallback a checker never takes."""
    tree = ast.parse("if TYPE_CHECKING:\n    from annotations_only import Shape\nelse:\n    import runtime_fallback\n")
    assert _import_time_top_levels(tree) == {"runtime_fallback"}
