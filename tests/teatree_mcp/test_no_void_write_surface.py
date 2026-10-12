import ast
from pathlib import Path

import teatree.mcp

_WRITER = "teatree.core.merge.void_merge"


def _reaches_the_void(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(_WRITER):
            return True
        if isinstance(node, ast.Import) and any(alias.name.startswith(_WRITER) for alias in node.names):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "void-merge" in node.value:
            return True
    return False


def test_no_mcp_module_reaches_the_merge_void_writer() -> None:
    package = Path(teatree.mcp.__file__).parent
    modules = sorted(package.rglob("*.py"))

    assert modules
    assert [str(path.relative_to(package)) for path in modules if _reaches_the_void(path)] == []
