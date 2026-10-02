"""A dependency whose SUBMODULE PATH we import must be bounded below the next major.

The lockfile pins CI, but the deployed container installs the CLI with
``uv tool install``, which re-resolves the declared constraint. An unbounded
``>=`` therefore lets production run a major the test suite never sees: the
tree imported ``mcp.server.fastmcp`` until mcp 2.0 removed that path (renamed
to ``mcp.server.mcpserver``, ``FastMCP`` -> ``MCPServer``), so the MCP server
crashed on import in the worker while every CI lane stayed green on 1.28.1.

Importing a submodule path is a much stronger coupling than importing a
package: majors relocate module trees routinely. The coupled set is DERIVED by
walking ``src/teatree`` for dotted imports of a declared dependency — a
hand-maintained list would only ever pin the bounds someone remembered to add,
so the NEXT unbounded coupling would red nothing.
"""

import ast
import importlib.util
import re

from tests.conformance._declared_dependencies import declared_dependencies, distributions_by_top_level
from tests.conformance._src_tree import src_modules

_UPPER_BOUND = re.compile(r"[<!=]")


def _imported_modules() -> set[str]:
    """Every absolute module path ``src/teatree`` imports, dotted ones included."""
    modules: set[str] = set()
    for _, tree in src_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def _submodule_coupled() -> dict[str, set[str]]:
    """Declared dependencies imported by submodule path, mapped to those imports."""
    declared = declared_dependencies()
    by_top_level = distributions_by_top_level()
    coupled: dict[str, set[str]] = {}
    for module in _imported_modules():
        top_level, _, rest = module.partition(".")
        if not rest:
            continue
        for dist in by_top_level.get(top_level, frozenset()) & declared.keys():
            coupled.setdefault(dist, set()).add(module)
    return coupled


def test_every_declared_dependency_is_mapped_to_its_import_name():
    """The walk is only as complete as the installed dist->module mapping."""
    mapped = {dist for dists in distributions_by_top_level().values() for dist in dists}
    unmapped = sorted(declared_dependencies().keys() - mapped)
    assert not unmapped, (
        f"declared dependencies with no installed top-level module, so the coupling walk cannot see them: {unmapped}"
    )


def test_submodule_coupled_dependencies_carry_an_upper_bound():
    declared = declared_dependencies()
    coupled = _submodule_coupled()
    unbounded = sorted(
        f"{name} ({declared[name]!r}) — the tree imports {', '.join(sorted(imports)[:3])}"
        for name, imports in coupled.items()
        if not _UPPER_BOUND.search(declared[name].split(">=", 1)[-1])
    )
    assert not unbounded, (
        "these dependencies are imported by submodule path but declared without an upper bound, "
        "so `uv tool install` can resolve a major the suite never runs against: " + "; ".join(unbounded)
    )


def test_the_coupled_imports_actually_resolve():
    """A bound is only meaningful while the import paths it protects still exist."""
    missing = sorted(
        module
        for imports in _submodule_coupled().values()
        for module in imports
        if importlib.util.find_spec(module) is None
    )
    assert not missing, f"imported submodule paths that no longer resolve in the installed env: {missing}"


def test_the_walk_sees_the_coupling_that_motivated_the_rule():
    """The control: a walk that saw nothing would pass every assertion above."""
    assert "mcp.server.mcpserver" in _submodule_coupled().get("mcp", set())
