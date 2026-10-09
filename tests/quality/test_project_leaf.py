"""Fitness function: ``teatree.project`` must stay a foundation LEAF (finding #2).

``find_project_root`` is re-exported from ``teatree.__init__`` as part of the
overlay API, so overlays import ``teatree.project`` transitively. If it grew an
import back into the rest of teatree (``teatree.core``, ``teatree.config``,
``teatree.cli``, …), an overlay's import of the overlay API would drag the whole
substrate in. The only internal edge it may carry is ``teatree.paths`` (path
resolution).

Two halves, mirroring ``test_import_contracts.py``:

``TestProjectLeafImports`` AST-scans ``src/teatree/project.py`` and asserts every
internal ``teatree.*`` import resolves to the allowed ``teatree.paths`` leaf — a
copy-pasted ``from teatree.core import …`` turns it red without waiting on tach.

``TestProjectTachDeclaration`` pins the ``tach.toml`` ``[[modules]]`` entry so the
constraint cannot be silently loosened (depends_on widened, layer raised, the
entry deleted back into the leaf allowlist).
"""

import ast
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_PY = _REPO_ROOT / "src" / "teatree" / "project.py"
_TACH = _REPO_ROOT / "tach.toml"

# The single internal edge teatree.project is allowed to carry.
_ALLOWED_INTERNAL = {"teatree.paths"}


def _internal_imports(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("teatree"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("teatree"):
                    found.add(alias.name)
    return found


def _top_level_module(dotted: str) -> str:
    parts = dotted.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else dotted


def _module_entry(path: str) -> dict[str, object]:
    data = tomllib.loads(_TACH.read_text(encoding="utf-8"))
    return next(m for m in data["modules"] if m["path"] == path)


class TestProjectLeafImports:
    def test_imports_only_the_allowed_leaf(self) -> None:
        forbidden = {imp for imp in _internal_imports(_PROJECT_PY) if _top_level_module(imp) not in _ALLOWED_INTERNAL}
        assert not forbidden, (
            f"teatree.project must stay a foundation leaf (overlay-API re-export). "
            f"It may only import {sorted(_ALLOWED_INTERNAL)}; found forbidden internal "
            f"imports: {sorted(forbidden)}"
        )


class TestProjectTachDeclaration:
    def test_declared_as_foundation_leaf(self) -> None:
        entry = _module_entry("teatree.project")
        assert entry["layer"] == "foundation"
        assert set(entry.get("depends_on", [])) == _ALLOWED_INTERNAL

    def test_not_in_leaf_allowlist(self) -> None:
        # A real [[modules]] entry constrains the edge; the leaf allowlist would
        # leave it unconstrained. It must not be in both.
        import scripts.hooks.check_tach_modules_declared as guard  # noqa: PLC0415

        assert "teatree.project" not in guard.LEAF_MODULE_ALLOWLIST
