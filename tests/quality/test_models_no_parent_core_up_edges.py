"""Fitness gate: ``core/models/`` has ZERO imports into parent-core packages (#2385 PR-2a).

PR-2a severed the 21 deferred (function-scoped) up-edges from ``teatree.core.models``
into the parent-core packages that depend ON the models — ``gates``, ``tasks``,
``worktree_tasks``, ``overlay_loader``, ``cost``, and ``backend_protocols``. They
were inverted through the ``post_transition`` signal layer (tasks / worktree_tasks)
and the ``modelkit.gate_registry`` (gates / resolvers / cost), and ``ReviewState``
was moved DOWN into ``modelkit``.

This gate is the structural guard that keeps an up-edge from creeping back: any
``import`` / ``from ... import`` inside ``core/models/`` whose target is one of the
forbidden parent-core modules — at ANY scope, module-level or function-scoped — is a
violation. Anti-vacuous: re-introducing any one severed edge (e.g. a deferred
``from teatree.core.tasks import execute_ship`` inside ``ship()``) turns this red.
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODELS_ROOT = _REPO_ROOT / "src" / "teatree" / "core" / "models"

# The parent-core packages that depend ON the models — a models import of any of
# these is the up-edge PR-2a severed. ``backend_protocols`` is included because
# ``ReviewState`` now lives in ``modelkit``; the models must read it from there,
# not from ``backend_protocols`` (which re-exports it but sits above the models).
_FORBIDDEN_PREFIXES = (
    "teatree.core.gates",
    "teatree.core.tasks",
    "teatree.core.worktree.worktree_tasks",
    "teatree.core.overlay_loader",
    "teatree.core.cost",
    "teatree.core.backend_protocols",
)


def _is_forbidden(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_PREFIXES)


def _type_checking_import_lines(tree: ast.Module) -> set[int]:
    """Line numbers of imports nested under an ``if TYPE_CHECKING:`` block.

    A type-only import is erased at runtime (annotation strings), so it is NOT
    an up-edge — it cannot form an intra-core cycle or trigger
    ``AppRegistryNotReady``. tach ignores it too. The gate guards RUNTIME edges.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            is_type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if is_type_checking:
                for child in ast.walk(node):
                    if isinstance(child, ast.Import | ast.ImportFrom):
                        lines.add(child.lineno)
    return lines


def _forbidden_imports(source: Path) -> list[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    type_only = _type_checking_import_lines(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and node.lineno in type_only:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden(node.module):
            hits.append(f"{source.relative_to(_REPO_ROOT)}:{node.lineno}  from {node.module} import ...")
        elif isinstance(node, ast.Import):
            hits.extend(
                f"{source.relative_to(_REPO_ROOT)}:{node.lineno}  import {alias.name}"
                for alias in node.names
                if _is_forbidden(alias.name)
            )
    return hits


class TestModelsHaveNoParentCoreUpEdges:
    def test_no_models_import_into_forbidden_parent_core(self) -> None:
        hits = [hit for py in sorted(_MODELS_ROOT.rglob("*.py")) for hit in _forbidden_imports(py)]
        assert not hits, (
            "a teatree.core.models module imports a parent-core package that depends ON the "
            "models — the #2385 PR-2a up-edge reappeared. Invert it via the post_transition "
            "signal layer (tasks/worktree_tasks) or modelkit.gate_registry (gates/resolvers/"
            "cost), or read ReviewState from teatree.core.modelkit.review_state:\n" + "\n".join(hits)
        )
