"""A dependency whose SUBMODULE PATH we import must be bounded below the next major.

The lockfile pins CI, but the deployed container installs the CLI with
``uv tool install``, which re-resolves the declared constraint. An unbounded
``>=`` therefore lets production run a major the test suite never sees: the
tree imported ``mcp.server.fastmcp`` until mcp 2.0 removed that path (renamed
to ``mcp.server.mcpserver``, ``FastMCP`` -> ``MCPServer``), so the MCP server
crashed on import in the worker while every CI lane stayed green on 1.28.1.

Importing a submodule path is a much stronger coupling than importing a
package: majors relocate module trees routinely. This walk pins the bound for
each such dependency, so the next unbounded one fails here rather than in
production.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Distributions the tree imports by SUBMODULE path, mapped to the import that
#: binds us to their layout. Each must carry an upper bound in `dependencies`.
SUBMODULE_COUPLED = {"mcp": "mcp.server.mcpserver"}

_UPPER_BOUND = re.compile(r"[<!=]")


def _declared_dependencies() -> dict[str, str]:
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for spec in raw["project"]["dependencies"]:
        name = re.split(r"[<>=!\[ ]", spec, maxsplit=1)[0].strip()
        out[name] = spec
    return out


def test_submodule_coupled_dependencies_are_declared():
    declared = _declared_dependencies()
    missing = sorted(name for name in SUBMODULE_COUPLED if name not in declared)
    assert not missing, f"tracked in SUBMODULE_COUPLED but absent from [project.dependencies]: {missing}"


def test_submodule_coupled_dependencies_carry_an_upper_bound():
    declared = _declared_dependencies()
    unbounded = sorted(
        f"{name} ({declared[name]!r}) — the tree imports {path}"
        for name, path in SUBMODULE_COUPLED.items()
        if name in declared and not _UPPER_BOUND.search(declared[name].split(">=", 1)[-1])
    )
    assert not unbounded, (
        "these dependencies are imported by submodule path but declared without an upper bound, "
        "so `uv tool install` can resolve a major the suite never runs against: " + "; ".join(unbounded)
    )


def test_the_coupled_import_actually_resolves():
    """The bound is only meaningful while the import it protects still works."""
    import mcp.server.mcpserver  # noqa: PLC0415 — the assertion IS that this import resolves

    assert mcp.server.mcpserver.MCPServer is not None
