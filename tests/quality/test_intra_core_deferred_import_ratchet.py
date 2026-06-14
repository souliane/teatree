"""Shrink-ratchet for intra-``teatree.core`` deferred (function-scoped) imports (#2385).

``teatree.core`` is one tach node holding ~85 loose files plus the ``models`` /
``managers`` / ``modelkit`` sub-packages. Inside a single node tach's acyclic
guard cannot see intra-node cycles, so the FSM transition bodies, gate
predicates, and task-signal injections route their intra-``core`` edges through
**function-scoped** (deferred) imports — invisible to tach, invisible to the
human reading the module header. The #2385 sub-layering carves the lowest leaves
into their own tach nodes so the acyclic guard applies *within* ``core``; until
that carve is complete (PR-2/PR-3 sever the ``models`` / ``managers`` up-edges),
this ratchet keeps the deferred-import count from growing.

It is a SHRINK-only peg: an AST walk counts every ``import``/``from`` whose
enclosing scope is a function/method (not module-level) and whose target module
starts with ``teatree.core``. ``current <= _FROZEN`` blocks growth; on
``current < _FROZEN`` the message instructs lowering the peg so the gain is
banked (mirroring ``test_module_health_ratchet.py`` and ``test_project_leaf.py``).

The companion ``TestCoreModelkitLayer`` pins the PR-1 carve itself: the
``teatree.core.modelkit`` leaf is declared as a ``depends_on = []`` domain node,
and the ONE top-level ``models -> gates`` edge PR-1 severs is gone — the
Fibonacci-backoff leaf was mis-filed under ``core/gates/`` and a model
(``local_stack_queue``) imported it from there; moving it into
``core/modelkit/`` cuts that edge. (The remaining ``ticket.py`` gate-predicate
edges into ``core/gates/`` are the FSM-transition bodies PR-2 severs, not PR-1.)
"""

import ast
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_ROOT = _REPO_ROOT / "src" / "teatree" / "core"
_MODELS_ROOT = _CORE_ROOT / "models"
_TACH = _REPO_ROOT / "tach.toml"

# Measured in-worktree after the PR-1 modelkit carve (the three moved files stay
# under teatree.core.*, so the count is unchanged from the #2385 plan's HEAD
# measurement). SHRINK-ONLY: lower this as PR-2/PR-3 convert deferred edges into
# declared tach edges; never raise it.
_FROZEN_INTRA_CORE_DEFERRED = 217


def _function_scoped_intra_core_imports(source: Path) -> int:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def in_function_scope(node: ast.AST) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
                return True
            cur = parents.get(cur)
        return False

    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("teatree.core") and in_function_scope(node):
                count += 1
        elif isinstance(node, ast.Import):
            count += sum(1 for alias in node.names if alias.name.startswith("teatree.core") and in_function_scope(node))
    return count


def _count_all() -> int:
    return sum(_function_scoped_intra_core_imports(py) for py in sorted(_CORE_ROOT.rglob("*.py")))


def _module_entry(path: str) -> dict[str, object]:
    data = tomllib.loads(_TACH.read_text(encoding="utf-8"))
    return next(m for m in data["modules"] if m["path"] == path)


def _model_files_importing(dotted: str) -> list[str]:
    hits: list[str] = []
    for py in sorted(_MODELS_ROOT.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(dotted):
                hits.append(f"{py.relative_to(_REPO_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                hits.extend(
                    f"{py.relative_to(_REPO_ROOT)}:{node.lineno}"
                    for alias in node.names
                    if alias.name.startswith(dotted)
                )
    return hits


class TestIntraCoreDeferredImportRatchet:
    def test_count_does_not_exceed_frozen(self) -> None:
        current = _count_all()
        assert current <= _FROZEN_INTRA_CORE_DEFERRED, (
            f"intra-teatree.core deferred (function-scoped) imports grew to {current}, "
            f"over the frozen ceiling {_FROZEN_INTRA_CORE_DEFERRED}. A new function-scoped "
            f"`from teatree.core... import` hides an intra-core edge from tach's acyclic "
            f"guard. Make the edge a declared tach sub-node edge (#2385) instead of "
            f"adding another deferred import."
        )

    def test_count_is_not_below_frozen(self) -> None:
        # Banks every reduction immediately: when an edge becomes a declared tach
        # sub-node edge, the count drops and the peg must follow it down so a
        # future regression cannot silently spend the gain.
        current = _count_all()
        assert current >= _FROZEN_INTRA_CORE_DEFERRED, (
            f"intra-teatree.core deferred imports dropped to {current}, below the frozen "
            f"ceiling {_FROZEN_INTRA_CORE_DEFERRED}. Lower _FROZEN_INTRA_CORE_DEFERRED to "
            f"{current} to bank the reduction."
        )


class TestCoreModelkitLayer:
    def test_modelkit_declared_as_depends_on_nothing_leaf(self) -> None:
        entry = _module_entry("teatree.core.modelkit")
        assert entry["layer"] == "domain"
        assert entry.get("depends_on", []) == []

    def test_parent_core_declares_modelkit_child(self) -> None:
        parent = _module_entry("teatree.core")
        assert "teatree.core.modelkit" in parent.get("depends_on", [])

    def test_models_no_longer_import_the_fibonacci_gate_leaf(self) -> None:
        # The single top-level models -> gates edge PR-1 severs: the Fibonacci
        # backoff leaf was mis-filed at teatree.core.gates.fibonacci and the
        # local_stack_queue model imported it from there. It now lives under
        # teatree.core.modelkit; a model reaching back to the gates path
        # resurrects the severed edge.
        hits = _model_files_importing("teatree.core.gates.fibonacci")
        assert not hits, (
            "a model imports the moved Fibonacci leaf from teatree.core.gates "
            "(the severed #2385 PR-1 edge):\n" + "\n".join(hits)
        )

    def test_fibonacci_leaf_lives_under_modelkit_not_gates(self) -> None:
        assert (_CORE_ROOT / "modelkit" / "fibonacci.py").is_file()
        assert not (_CORE_ROOT / "gates" / "fibonacci.py").exists()
